#####################################################################
# THIS FILE IS AUTOMATICALLY GENERATED BY UNSTRUCTURED API TOOLS.
# DO NOT MODIFY DIRECTLY
#####################################################################

import io
import os
import gzip
import mimetypes
from typing import List, Union
from fastapi import status, FastAPI, File, Form, Request, UploadFile, APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
import json
from fastapi.responses import StreamingResponse
from starlette.datastructures import Headers
from starlette.types import Send
from base64 import b64encode
from typing import Optional, Mapping, Iterator, Tuple
import secrets
from unstructured.partition.auto import partition
from unstructured.staging.base import convert_to_isd
import tempfile
import pdfminer


app = FastAPI()
router = APIRouter()


def is_expected_response_type(media_type, response_type):
    if media_type == "application/json" and response_type not in [dict, list]:
        return True
    elif media_type == "text/csv" and response_type != str:
        return True
    else:
        return False


# pipeline-api


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
)

if not os.environ.get("UNSTRUCTURED_ALLOWED_MIMETYPES", None):
    os.environ["UNSTRUCTURED_ALLOWED_MIMETYPES"] = DEFAULT_MIMETYPES


def pipeline_api(
    file,
    filename="",
    m_strategy=[],
    m_coordinates=[],
    file_content_type=None,
    response_type="application/json",
):
    strategy = (m_strategy[0] if len(m_strategy) else "fast").lower()
    if strategy not in ["fast", "hi_res"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid strategy: {strategy}. Must be one of ['fast', 'hi_res']",
        )

    show_coordinates_str = (m_coordinates[0] if len(m_coordinates) else "false").lower()
    show_coordinates = show_coordinates_str == "true"

    if filename.endswith((".docx", ".pptx")):
        # NOTE(robinson) - This is a hacky solution due to
        # limitations in the SpooledTemporaryFile wrapper.
        # Specifically, it doesn't have a `seekable` attribute,
        # which is required for .pptx and .docx. See below
        # the link below
        # ref: https://stackoverflow.com/questions/47160211
        # /why-doesnt-tempfile-spooledtemporaryfile-implement-readable-writable-seekable
        with tempfile.TemporaryDirectory() as tmpdir:
            _filename = os.path.join(tmpdir, filename.split("/")[-1])
            with open(_filename, "wb") as f:
                f.write(file.read())
            elements = partition(filename=_filename, strategy=strategy)
    else:
        try:
            elements = partition(
                file=file, file_filename=filename, content_type=file_content_type, strategy=strategy
            )
        except ValueError as e:
            if "Invalid file" in e.args[0]:
                raise HTTPException(
                    status_code=400, detail=f"{file_content_type} not currently supported"
                )
            raise e
        except pdfminer.pdfparser.PDFSyntaxError:
            raise HTTPException(
                status_code=400, detail=f"{filename} does not appear to be a valid PDF"
            )

    # Due to the above, elements have an ugly temp filename in their metadata
    # For now, replace this with the basename
    result = convert_to_isd(elements)
    for element in result:
        element["metadata"]["filename"] = os.path.basename(filename)

        if not show_coordinates:
            del element["coordinates"]

    return result


def get_validated_mimetype(file):
    """
    Return a file's mimetype, either via the file.content_type or the mimetypes lib if that's too
    generic. If the user has set UNSTRUCTURED_ALLOWED_MIMETYPES, validate against this list and
    return HTTP 400 for an invalid type.
    """
    content_type = file.content_type
    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(str(file.filename))[0]

        # Some filetypes missing for this library, just hardcode them for now
        if not content_type:
            if file.filename.endswith(".md"):
                content_type = "text/markdown"
            elif file.filename.endswith(".msg"):
                content_type = "message/rfc822"

    allowed_mimetypes_str = os.environ.get("UNSTRUCTURED_ALLOWED_MIMETYPES")
    if allowed_mimetypes_str is not None:
        allowed_mimetypes = allowed_mimetypes_str.split(",")

        if content_type not in allowed_mimetypes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unable to process {file.filename}: "
                    f"File type {content_type} is not supported."
                ),
            )

    return content_type


class MultipartMixedResponse(StreamingResponse):
    CRLF = b"\r\n"

    def __init__(self, *args, content_type: str = None, **kwargs):
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

    def _build_part_headers(self, headers: dict) -> bytes:
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


def ungz_file(file: UploadFile, gz_uncompressed_content_type=None) -> UploadFile:
    def return_content_type(filename):
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


@router.post("/general/v0/general")
@router.post("/general/v0.0.17/general")
def pipeline_1(
    request: Request,
    gz_uncompressed_content_type: Optional[str] = Form(default=None),
    files: Union[List[UploadFile], None] = File(default=None),
    output_format: Union[str, None] = Form(default=None),
    strategy: List[str] = Form(default=[]),
    coordinates: List[str] = Form(default=[]),
):
    if files:
        for file_index in range(len(files)):
            if files[file_index].content_type == "application/gzip":
                files[file_index] = ungz_file(files[file_index], gz_uncompressed_content_type)

    content_type = request.headers.get("Accept")

    default_response_type = output_format or "application/json"
    if not content_type or content_type == "*/*" or content_type == "multipart/mixed":
        media_type = default_response_type
    else:
        media_type = content_type

    if isinstance(files, list) and len(files):
        if len(files) > 1:
            if content_type and content_type not in ["*/*", "multipart/mixed", "application/json"]:
                raise HTTPException(
                    detail=(
                        f"Conflict in media type {content_type}"
                        ' with response type "multipart/mixed".\n'
                    ),
                    status_code=status.HTTP_406_NOT_ACCEPTABLE,
                )

        def response_generator(is_multipart):
            for file in files:
                file_content_type = get_validated_mimetype(file)

                _file = file.file

                response = pipeline_api(
                    _file,
                    m_strategy=strategy,
                    m_coordinates=coordinates,
                    response_type=media_type,
                    filename=file.filename,
                    file_content_type=file_content_type,
                )

                if is_expected_response_type(media_type, type(response)):
                    raise HTTPException(
                        detail=(
                            f"Conflict in media type {media_type}"
                            f" with response type {type(response)}.\n"
                        ),
                        status_code=status.HTTP_406_NOT_ACCEPTABLE,
                    )

                valid_response_types = ["application/json", "text/csv", "*/*", "multipart/mixed"]
                if media_type in valid_response_types:
                    if is_multipart:
                        if type(response) not in [str, bytes]:
                            response = json.dumps(response)
                    yield response
                else:
                    raise HTTPException(
                        detail=f"Unsupported media type {media_type}.\n",
                        status_code=status.HTTP_406_NOT_ACCEPTABLE,
                    )

        if content_type == "multipart/mixed":
            return MultipartMixedResponse(
                response_generator(is_multipart=True), content_type=media_type
            )
        else:
            return (
                list(response_generator(is_multipart=False))[0]
                if len(files) == 1
                else response_generator(is_multipart=False)
            )
    else:
        raise HTTPException(
            detail='Request parameter "files" is required.\n',
            status_code=status.HTTP_400_BAD_REQUEST,
        )


app.include_router(router)
