from __future__ import annotations

import gzip
import io
import json
import logging
import mimetypes
import os
import secrets
import zipfile
from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from types import TracebackType
from typing import IO, Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union, cast

import backoff
import pandas as pd
import psutil
import requests
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import PlainTextResponse, StreamingResponse
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.errors import FileNotDecryptedError, PdfReadError
from starlette.datastructures import Headers
from starlette.types import Send
from unstructured.documents.elements import Element
from unstructured.partition.auto import partition
from unstructured.staging.base import (
    convert_to_dataframe,
    convert_to_isd,
    elements_from_json,
)
from unstructured_inference.models.base import UnknownModelException
from unstructured_inference.models.chipper import MODEL_TYPES as CHIPPER_MODEL_TYPES

from prepline_general.api.models.form_params import GeneralFormParams

app = FastAPI()
router = APIRouter()


def is_compatible_response_type(media_type: str, response_type: type) -> bool:
    """True when `response_type` can be converted to `media_type` for HTTP Response."""
    return (
        False
        if media_type == "application/json" and response_type not in [dict, list]
        else False if media_type == "text/csv" and response_type != str else True
    )


logger = logging.getLogger("unstructured_api")

DEFAULT_MIMETYPES = (
    "application/pdf,application/msword,image/jpeg,image/png,text/markdown,"
    "text/x-markdown,text/html,"
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
    "application/vnd.ms-excel,application/vnd.openxmlformats-officedocument."
    "presentationml.presentation,"
    "application/json,"
    "application/vnd.ms-powerpoint,"
    "text/html,message/rfc822,text/plain,image/png,"
    "application/epub,application/epub+zip,"
    "application/rtf,text/rtf,"
    "application/vnd.oasis.opendocument.text,"
    "text/csv,text/x-csv,application/csv,application/x-csv,"
    "text/comma-separated-values,text/x-comma-separated-values,"
    "application/xml,text/xml,text/x-rst,text/prs.fallenstein.rst,"
    "text/tsv,text/tab-separated-values,"
    "application/x-ole-storage,application/vnd.ms-outlook,"
    "application/yaml,"
    "application/x-yaml,"
    "text/x-yaml,"
    "text/yaml,"
)

if not os.environ.get("UNSTRUCTURED_ALLOWED_MIMETYPES", None):
    os.environ["UNSTRUCTURED_ALLOWED_MIMETYPES"] = DEFAULT_MIMETYPES


def get_pdf_splits(pdf_pages: Sequence[PageObject], split_size: int = 1):
    """Given a pdf (PdfReader) with n pages, split it into pdfs each with split_size # of pages.

    Return the files with their page offset in the form [(BytesIO, int)]
    """
    offset = 0

    while offset < len(pdf_pages):
        new_pdf = PdfWriter()
        pdf_buffer = io.BytesIO()

        end = offset + split_size
        for page in pdf_pages[offset:end]:
            new_pdf.add_page(page)

        new_pdf.write(pdf_buffer)
        pdf_buffer.seek(0)

        yield (pdf_buffer.read(), offset)
        offset += split_size


# Do not retry with these status codes
def is_non_retryable(e: Exception) -> bool:
    # -- `Exception` doesn't have a `.status_code` attribute so the check of status-code would
    # -- itself raise `AttributeError` when e is say ValueError or TypeError, etc.
    if not isinstance(e, HTTPException):
        return True
    return 400 <= e.status_code < 500


@backoff.on_exception(
    backoff.expo,
    HTTPException,
    max_tries=int(os.environ.get("UNSTRUCTURED_PARALLEL_RETRY_ATTEMPTS", 2)) + 1,
    giveup=is_non_retryable,
    logger=logger,
)
def call_api(
    request_url: str,
    api_key: str,
    filename: str,
    file: IO[bytes],
    content_type: str,
    **partition_kwargs: Any,
) -> str:
    """Call the api with the given request_url."""
    headers = {"unstructured-api-key": api_key}

    response = requests.post(
        request_url,
        files={"files": (filename, file, content_type)},
        data=partition_kwargs,
        headers=headers,
    )

    if response.status_code != 200:
        detail = response.json().get("detail") or response.text
        raise HTTPException(status_code=response.status_code, detail=detail)

    return response.text


def partition_file_via_api(
    file_tuple: Tuple[IO[bytes], int],
    request: Request,
    filename: str,
    content_type: str,
    **partition_kwargs: Any,
) -> List[Element]:
    """Send the given file to be partitioned remotely with retry logic.

    The remote url is set by the `UNSTRUCTURED_PARALLEL_MODE_URL` environment variable.

    Args:
    `file_tuple` is a file-like object and byte offset of a page (file, page_offest)
    `request` is used to forward the api key header
    `filename` and `content_type` are passed in the file form data
    `partition_kwargs` holds any form parameters to be sent on
    """
    file, page_offset = file_tuple

    request_url = os.environ.get("UNSTRUCTURED_PARALLEL_MODE_URL")
    if not request_url:
        raise HTTPException(status_code=500, detail="Parallel mode enabled but no url set!")

    api_key = request.headers.get("unstructured-api-key", default="")

    result = call_api(request_url, api_key, filename, file, content_type, **partition_kwargs)
    elements = elements_from_json(text=result)

    # We need to account for the original page numbers
    for element in elements:
        if element.metadata.page_number:
            # Page number could be None if we include page breaks
            element.metadata.page_number += page_offset

    return elements


def partition_pdf_splits(
    request: Request,
    pdf_pages: Sequence[PageObject],
    file: IO[bytes],
    metadata_filename: str,
    content_type: str,
    coordinates: bool,
    **partition_kwargs: Any,
) -> List[Element]:
    """Split a pdf into chunks and process in parallel with more api calls.

    Or partition locally if the chunk is small enough. As soon as any remote call fails, bubble up
    the error.

    Arguments:
    request is used to forward relevant headers to the api calls
    file, metadata_filename and content_type are passed on in the file argument to requests.post
    coordinates is passed on to the api calls, but cannot be used in the local partition case
    partition_kwargs holds any others parameters that will be forwarded, or passed to partition
    """
    pages_per_pdf = int(os.environ.get("UNSTRUCTURED_PARALLEL_MODE_SPLIT_SIZE", 1))

    # If it's small enough, just process locally
    if len(pdf_pages) <= pages_per_pdf:
        return partition(
            file=file,
            metadata_filename=metadata_filename,
            content_type=content_type,
            **partition_kwargs,
        )

    results: List[Element] = []
    page_iterator = get_pdf_splits(pdf_pages, split_size=pages_per_pdf)

    partition_func = partial(
        partition_file_via_api,
        request=request,
        filename=metadata_filename,
        content_type=content_type,
        coordinates=coordinates,
        **partition_kwargs,
    )

    thread_count = int(os.environ.get("UNSTRUCTURED_PARALLEL_MODE_THREADS", 3))
    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        for result in executor.map(partition_func, page_iterator):
            results.extend(result)

    return results


is_chipper_processing = False


class ChipperMemoryProtection:
    """Chipper calls are expensive, and right now we can only do one call at a time.

    If the model is in use, return a 503 error. The API should scale up and the user can try again
    on a different server.
    """

    def __enter__(self):
        global is_chipper_processing
        if is_chipper_processing:
            # Log here so we can track how often it happens
            logger.error("Chipper is already is use")
            raise HTTPException(
                status_code=503, detail="Server is under heavy load. Please try again later."
            )

        is_chipper_processing = True

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ):
        global is_chipper_processing
        is_chipper_processing = False


def pipeline_api(
    file: IO[bytes],
    request: Request,
    # -- chunking options --
    chunking_strategy: Optional[str],
    combine_under_n_chars: Optional[int],
    max_characters: int,
    multipage_sections: bool,
    new_after_n_chars: Optional[int],
    # ----------------------
    filename: str = "",
    file_content_type: Optional[str] = None,
    response_type: str = "application/json",
    coordinates: bool = False,
    encoding: str = "utf-8",
    hi_res_model_name: Optional[str] = None,
    include_page_breaks: bool = False,
    ocr_languages: Optional[List[str]] = None,
    pdf_infer_table_structure: bool = False,
    skip_infer_table_types: Optional[List[str]] = None,
    strategy: str = "auto",
    xml_keep_tags: bool = False,
    languages: Optional[List[str]] = None,
    extract_image_block_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]] | str:
    if filename.endswith(".msg"):
        # Note(yuming): convert file type for msg files
        # since fast api might sent the wrong one.
        file_content_type = "application/x-ole-storage"

    # We don't want to keep logging the same params for every parallel call
    is_internal_request = (
        (
            request.headers.get("X-Forwarded-For")
            and str(request.headers.get("X-Forwarded-For")).startswith("10.")
        )
        # -- NOTE(scanny): request.client is None in certain testing environments --
        or (request.client and request.client.host.startswith("10."))
    )

    if not is_internal_request:
        logger.debug(
            "pipeline_api input params: {}".format(
                json.dumps(
                    {
                        "filename": filename,
                        "response_type": response_type,
                        "coordinates": coordinates,
                        "encoding": encoding,
                        "hi_res_model_name": hi_res_model_name,
                        "include_page_breaks": include_page_breaks,
                        "ocr_languages": ocr_languages,
                        "pdf_infer_table_structure": pdf_infer_table_structure,
                        "skip_infer_table_types": skip_infer_table_types,
                        "strategy": strategy,
                        "xml_keep_tags": xml_keep_tags,
                        "languages": languages,
                        "extract_image_block_types": extract_image_block_types,
                        "chunking_strategy": chunking_strategy,
                        "combine_under_n_chars": combine_under_n_chars,
                        "max_characters": max_characters,
                        "multipage_sections": multipage_sections,
                        "new_after_n_chars": new_after_n_chars,
                    },
                    default=str,
                )
            )
        )

        logger.debug(f"filetype: {file_content_type}")

    _check_free_memory()

    if file_content_type == "application/pdf":
        _check_pdf(file)

    hi_res_model_name = _validate_hi_res_model_name(hi_res_model_name, coordinates)
    strategy = _validate_strategy(strategy)
    pdf_infer_table_structure = _set_pdf_infer_table_structure(pdf_infer_table_structure, strategy)

    # Parallel mode is set by env variable
    enable_parallel_mode = os.environ.get("UNSTRUCTURED_PARALLEL_MODE_ENABLED", "false")
    pdf_parallel_mode_enabled = enable_parallel_mode == "true"

    ocr_languages_str = "+".join(ocr_languages) if ocr_languages and len(ocr_languages) else None

    if extract_image_block_types:
        try:
            # Handle the case when the user passes the table of strings as a json inside the
            # first element of the array
            loaded_array = json.loads(extract_image_block_types[0])
            if isinstance(loaded_array, list):
                extract_image_block_types = loaded_array
        except (json.JSONDecodeError, IndexError):
            pass  # noqa

    extract_image_block_to_payload = bool(extract_image_block_types)

    try:
        logger.debug(
            "partition input data: {}".format(
                json.dumps(
                    {
                        "content_type": file_content_type,
                        "strategy": strategy,
                        "ocr_languages": ocr_languages_str,
                        "coordinates": coordinates,
                        "pdf_infer_table_structure": pdf_infer_table_structure,
                        "include_page_breaks": include_page_breaks,
                        "encoding": encoding,
                        "hi_res_model_name": hi_res_model_name,
                        "xml_keep_tags": xml_keep_tags,
                        "skip_infer_table_types": skip_infer_table_types,
                        "languages": languages,
                        "chunking_strategy": chunking_strategy,
                        "multipage_sections": multipage_sections,
                        "combine_under_n_chars": combine_under_n_chars,
                        "new_after_n_chars": new_after_n_chars,
                        "max_characters": max_characters,
                        "extract_image_block_types": extract_image_block_types,
                        "extract_image_block_to_payload": extract_image_block_to_payload,
                    },
                    default=str,
                )
            )
        )

        partition_kwargs = {
            "file": file,
            "metadata_filename": filename,
            "content_type": file_content_type,
            "encoding": encoding,
            "include_page_breaks": include_page_breaks,
            "hi_res_model_name": hi_res_model_name,
            "ocr_languages": ocr_languages_str,
            "pdf_infer_table_structure": pdf_infer_table_structure,
            "skip_infer_table_types": skip_infer_table_types,
            "strategy": strategy,
            "xml_keep_tags": xml_keep_tags,
            "languages": languages,
            "chunking_strategy": chunking_strategy,
            "multipage_sections": multipage_sections,
            "combine_text_under_n_chars": combine_under_n_chars,
            "new_after_n_chars": new_after_n_chars,
            "max_characters": max_characters,
            "extract_image_block_types": extract_image_block_types,
            "extract_image_block_to_payload": extract_image_block_to_payload,
        }

        if file_content_type == "application/pdf" and pdf_parallel_mode_enabled:
            pdf = PdfReader(file)
            elements = partition_pdf_splits(
                request=request,
                pdf_pages=pdf.pages,
                coordinates=coordinates,
                **partition_kwargs,  # type: ignore # pyright: ignore[reportGeneralTypeIssues]
            )
        elif hi_res_model_name and hi_res_model_name in CHIPPER_MODEL_TYPES:
            with ChipperMemoryProtection():
                elements = partition(**partition_kwargs)  # pyright: ignore[reportGeneralTypeIssues]
        else:
            elements = partition(**partition_kwargs)  # pyright: ignore[reportGeneralTypeIssues]

    except OSError as e:
        if isinstance(e.args[0], str) and (
            "chipper-fast-fine-tuning is not a local folder" in e.args[0]
            or "ved-fine-tuning is not a local folder" in e.args[0]
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    "The Chipper model is not available for download. It can be accessed via the"
                    " official hosted API."
                ),
            )

        # OSError isn't caught by our top level handler, so convert it here
        raise HTTPException(
            status_code=500,
            detail=str(e),
        )
    except ValueError as e:
        if "Invalid file" in e.args[0]:
            raise HTTPException(
                status_code=400, detail=f"{file_content_type} not currently supported"
            )
        if "Unstructured schema" in e.args[0]:
            raise HTTPException(
                status_code=400,
                detail="Json schema does not match the Unstructured schema",
            )
        if "fast strategy is not available for image files" in e.args[0]:
            raise HTTPException(
                status_code=400,
                detail="The fast strategy is not available for image files",
            )

        raise e
    except zipfile.BadZipFile:
        raise HTTPException(
            status_code=422,
            detail="File is not a valid docx",
        )

    except UnknownModelException:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model type: {hi_res_model_name}",
        )

    # Clean up returned elements
    # Note(austin): pydantic should control this sort of thing for us
    for i, element in enumerate(elements):
        elements[i].metadata.filename = os.path.basename(filename)

        if not coordinates and element.metadata.coordinates:
            elements[i].metadata.coordinates = None

        if element.metadata.last_modified:
            elements[i].metadata.last_modified = None

        if element.metadata.file_directory:
            elements[i].metadata.file_directory = None

        if element.metadata.detection_class_prob:
            elements[i].metadata.detection_class_prob = None

    if response_type == "text/csv":
        df = convert_to_dataframe(elements)
        return df.to_csv(index=False)

    result = convert_to_isd(elements)

    return result


def _check_free_memory():
    """Reject traffic when free memory is below minimum (default 2GB)."""
    mem = psutil.virtual_memory()
    memory_free_minimum = int(os.environ.get("UNSTRUCTURED_MEMORY_FREE_MINIMUM_MB", 2048))

    if mem.available <= memory_free_minimum * 1024 * 1024:
        logger.warning(f"Rejecting because free memory is below {memory_free_minimum} MB")
        raise HTTPException(
            status_code=503, detail="Server is under heavy load. Please try again later."
        )


def _check_pdf(file: IO[bytes]):
    """Check if the PDF file is encrypted, otherwise assume it is not a valid PDF."""
    try:
        pdf = PdfReader(file)

        # This will raise if the file is encrypted
        pdf.metadata
        return pdf
    except FileNotDecryptedError:
        raise HTTPException(
            status_code=400,
            detail="File is encrypted. Please decrypt it with password.",
        )
    except PdfReadError:
        raise HTTPException(status_code=422, detail="File does not appear to be a valid PDF")


def _validate_strategy(strategy: str) -> str:
    strategy = strategy.lower()
    strategies = ["fast", "hi_res", "auto", "ocr_only"]
    if strategy not in strategies:
        raise HTTPException(
            status_code=400, detail=f"Invalid strategy: {strategy}. Must be one of {strategies}"
        )
    return strategy


def _validate_hi_res_model_name(
    hi_res_model_name: Optional[str], show_coordinates: bool
) -> Optional[str]:
    # Make sure chipper aliases to the latest model
    if hi_res_model_name and hi_res_model_name == "chipper":
        hi_res_model_name = "chipperv2"

    if hi_res_model_name and hi_res_model_name in CHIPPER_MODEL_TYPES and show_coordinates:
        raise HTTPException(
            status_code=400,
            detail=f"coordinates aren't available when using the {hi_res_model_name} model type",
        )
    return hi_res_model_name


def _validate_chunking_strategy(chunking_strategy: Optional[str]) -> Optional[str]:
    """Raise on `chunking_strategy` is not a valid chunking strategy name.

    Also provides case-insensitivity.
    """
    if chunking_strategy is None:
        return None

    chunking_strategy = chunking_strategy.lower()
    available_strategies = ["basic", "by_title"]

    if chunking_strategy not in available_strategies:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid chunking strategy: {chunking_strategy}. Must be one of"
                f" {available_strategies}"
            ),
        )

    return chunking_strategy


def _set_pdf_infer_table_structure(pdf_infer_table_structure: bool, strategy: str) -> bool:
    return strategy == "hi_res" and pdf_infer_table_structure


def get_validated_mimetype(file: UploadFile) -> Optional[str]:
    """The MIME-type of `file`.

    The mimetype is computed based on `file.content_type`, or the mimetypes lib if that's too
    generic. If the user has set UNSTRUCTURED_ALLOWED_MIMETYPES, validate against this list and
    return HTTP 400 for an invalid type.
    """
    content_type = file.content_type
    filename = str(file.filename)  # -- "None" when file.filename is None --
    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(filename)[0]

        # Some filetypes missing for this library, just hardcode them for now
        if not content_type:
            if filename.endswith(".md"):
                content_type = "text/markdown"
            elif filename.endswith(".msg"):
                content_type = "message/rfc822"

    allowed_mimetypes_str = os.environ.get("UNSTRUCTURED_ALLOWED_MIMETYPES")
    if allowed_mimetypes_str is not None:
        allowed_mimetypes = allowed_mimetypes_str.split(",")

        if content_type not in allowed_mimetypes:
            raise HTTPException(
                status_code=400,
                detail=(f"File type {content_type} is not supported."),
            )

    return content_type


class MultipartMixedResponse(StreamingResponse):
    CRLF = b"\r\n"

    def __init__(self, *args: Any, content_type: Optional[str] = None, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.content_type = content_type

    def init_headers(self, headers: Optional[Mapping[str, str]] = None) -> None:
        super().init_headers(headers)
        self.boundary_value = secrets.token_hex(16)
        content_type = f'multipart/mixed; boundary="{self.boundary_value}"'
        self.raw_headers.append((b"content-type", content_type.encode("latin-1")))

    @property
    def boundary(self):
        return b"--" + self.boundary_value.encode()

    def _build_part_headers(self, headers: Dict[str, Any]) -> bytes:
        header_bytes = b""
        for header, value in headers.items():
            header_bytes += f"{header}: {value}".encode() + self.CRLF
        return header_bytes

    def build_part(self, chunk: bytes) -> bytes:
        part = self.boundary + self.CRLF
        part_headers = {"Content-Length": len(chunk), "Content-Transfer-Encoding": "base64"}
        if self.content_type is not None:
            part_headers["Content-Type"] = self.content_type
        part += self._build_part_headers(part_headers)
        part += self.CRLF + chunk + self.CRLF
        return part

    async def stream_response(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        async for chunk in self.body_iterator:
            if not isinstance(chunk, bytes):
                chunk = chunk.encode(self.charset)
                chunk = b64encode(chunk)
            await send(
                {"type": "http.response.body", "body": self.build_part(chunk), "more_body": True}
            )

        await send({"type": "http.response.body", "body": b"", "more_body": False})


def ungz_file(file: UploadFile, gz_uncompressed_content_type: Optional[str] = None) -> UploadFile:
    def return_content_type(filename: str):
        if gz_uncompressed_content_type:
            return gz_uncompressed_content_type
        else:
            return str(mimetypes.guess_type(filename)[0])

    filename = str(file.filename) if file.filename else ""
    if filename.endswith(".gz"):
        filename = filename[:-3]

    gzip_file = gzip.open(file.file).read()
    return UploadFile(
        file=io.BytesIO(gzip_file),
        size=len(gzip_file),
        filename=filename,
        headers=Headers({"content-type": return_content_type(filename)}),
    )


@router.get("/general/v0/general", include_in_schema=False)
@router.get("/general/v0.0.63/general", include_in_schema=False)
async def handle_invalid_get_request():
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED, detail="Only POST requests are supported."
    )


@router.post(
    "/general/v0/general",
    openapi_extra={"x-speakeasy-name-override": "partition"},
    tags=["general"],
    summary="Summary",
    description="Description",
)
@router.post("/general/v0.0.63/general", include_in_schema=False)
def general_partition(
    request: Request,
    # cannot use annotated type here because of a bug described here:
    # https://github.com/tiangolo/fastapi/discussions/10280
    # The openapi metadata must be added separately in openapi.py file.
    # TODO: Check if the bug is fixed and change the declaration to use Annoteted[List[UploadFile], File(...)]
    files: List[UploadFile],
    form_params: GeneralFormParams = Depends(GeneralFormParams.as_form),
):
    # -- must have a valid API key --
    if api_key_env := os.environ.get("UNSTRUCTURED_API_KEY"):
        api_key = request.headers.get("unstructured-api-key")
        if api_key != api_key_env:
            raise HTTPException(
                detail=f"API key {api_key} is invalid", status_code=status.HTTP_401_UNAUTHORIZED
            )

    content_type = request.headers.get("Accept")

    # -- detect response content-type conflict when multiple files are uploaded --
    if (
        len(files) > 1
        and content_type
        and content_type
        not in [
            "*/*",
            "multipart/mixed",
            "application/json",
            "text/csv",
        ]
    ):
        raise HTTPException(
            detail=f"Conflict in media type {content_type} with response type 'multipart/mixed'.\n",
            status_code=status.HTTP_406_NOT_ACCEPTABLE,
        )

    # -- validate other arguments --
    chunking_strategy = _validate_chunking_strategy(form_params.chunking_strategy)

    # -- unzip any uploaded files that need it --
    for file_index in range(len(files)):
        if files[file_index].content_type == "application/gzip":
            files[file_index] = ungz_file(
                files[file_index], form_params.gz_uncompressed_content_type
            )

    default_response_type = form_params.output_format or "application/json"
    if not content_type or content_type == "*/*" or content_type == "multipart/mixed":
        media_type = default_response_type
    else:
        media_type = content_type

    def response_generator(is_multipart: bool):
        for file in files:
            file_content_type = get_validated_mimetype(file)

            _file = file.file

            response = pipeline_api(
                _file,
                request=request,
                coordinates=form_params.coordinates,
                encoding=form_params.encoding,
                hi_res_model_name=form_params.hi_res_model_name,
                include_page_breaks=form_params.include_page_breaks,
                ocr_languages=form_params.ocr_languages,
                pdf_infer_table_structure=form_params.pdf_infer_table_structure,
                skip_infer_table_types=form_params.skip_infer_table_types,
                strategy=form_params.strategy,
                xml_keep_tags=form_params.xml_keep_tags,
                response_type=media_type,
                filename=str(file.filename),
                file_content_type=file_content_type,
                languages=form_params.languages,
                extract_image_block_types=form_params.extract_image_block_types,
                # -- chunking options --
                chunking_strategy=chunking_strategy,
                combine_under_n_chars=form_params.combine_under_n_chars,
                max_characters=form_params.max_characters,
                multipage_sections=form_params.multipage_sections,
                new_after_n_chars=form_params.new_after_n_chars,
            )

            if not is_compatible_response_type(media_type, type(response)):
                raise HTTPException(
                    detail=(
                        f"Conflict in media type {media_type}"
                        f" with response type {type(response)}.\n"
                    ),
                    status_code=status.HTTP_406_NOT_ACCEPTABLE,
                )

            if media_type not in ["application/json", "text/csv", "*/*", "multipart/mixed"]:
                raise HTTPException(
                    detail=f"Unsupported media type {media_type}.\n",
                    status_code=status.HTTP_406_NOT_ACCEPTABLE,
                )

            yield (
                json.dumps(response)
                if is_multipart and type(response) not in [str, bytes]
                else (
                    PlainTextResponse(response)
                    if not is_multipart and media_type == "text/csv"
                    else response
                )
            )

    def join_responses(
        responses: Sequence[str | List[Dict[str, Any]] | PlainTextResponse]
    ) -> List[str | List[Dict[str, Any]]] | PlainTextResponse:
        """Consolidate partitionings from multiple documents into single response payload."""
        if media_type != "text/csv":
            return cast(List[Union[str, List[Dict[str, Any]]]], responses)
        responses = cast(List[PlainTextResponse], responses)
        data = pd.read_csv(  # pyright: ignore[reportUnknownMemberType]
            io.BytesIO(responses[0].body)
        )
        if len(responses) > 1:
            for resp in responses[1:]:
                resp_data = pd.read_csv(  # pyright: ignore[reportUnknownMemberType]
                    io.BytesIO(resp.body)
                )
                data = data.merge(  # pyright: ignore[reportUnknownMemberType]
                    resp_data, how="outer"
                )
        return PlainTextResponse(data.to_csv())

    return (
        MultipartMixedResponse(response_generator(is_multipart=True), content_type=media_type)
        if content_type == "multipart/mixed"
        else (
            list(response_generator(is_multipart=False))[0]
            if len(files) == 1
            else join_responses(list(response_generator(is_multipart=False)))
        )
    )


app.include_router(router)
