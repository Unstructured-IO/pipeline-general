from pathlib import Path
import os

import io
import pytest
import requests
import time
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from fastapi import HTTPException
from pypdf import PdfWriter, PdfReader
from unittest.mock import Mock, ANY

from prepline_general.api.app import app
from prepline_general.api import general
import tempfile

MAIN_API_ROUTE = "general/v0/general"


def test_general_api_health_check():
    client = TestClient(app)
    response = client.get("/healthcheck")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "example_filename, content_type",
    [
        # Note(yuming): Please sort filetypes alphabetically according to
        # https://github.com/Unstructured-IO/unstructured/blob/main/unstructured/partition/auto.py#L14
        ("stanley-cups.csv", "application/csv"),
        ("fake.doc", "application/msword"),
        ("fake.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("family-day.eml", "message/rfc822"),
        ("alert.eml", "message/rfc822"),
        ("announcement.eml", "message/rfc822"),
        ("fake-email-attachment.eml", "message/rfc822"),
        ("fake-email-image-embedded.eml", "message/rfc822"),
        ("fake-email.eml", "message/rfc822"),
        ("winter-sports.epub", "application/epub"),
        ("fake-html.html", "text/html"),
        ("layout-parser-paper-fast.jpg", "image/jpeg"),
        ("spring-weather.html.json", "application/json"),
        ("README.md", "text/markdown"),
        ("fake-email.msg", "application/x-ole-storage"),
        ("fake.odt", "application/vnd.oasis.opendocument.text"),
        ("layout-parser-paper.pdf", "application/pdf"),
        ("fake-power-point.ppt", "application/vnd.ms-powerpoint"),
        (
            "fake-power-point.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        ("README.rst", "text/x-rst"),
        ("fake-doc.rtf", "application/rtf"),
        ("fake-text.txt", "text/plain"),
        ("stanley-cups.tsv", "text/tsv"),
        (
            "stanley-cups.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        ("fake-xml.xml", "application/xml"),
    ],
)
def test_general_api(example_filename, content_type):
    client = TestClient(app)
    test_file = Path("sample-docs") / example_filename
    response = client.post(
        MAIN_API_ROUTE, files=[("files", (str(test_file), open(test_file, "rb"), content_type))]
    )
    assert response.status_code == 200
    assert len(response.json()) > 0
    for i in response.json():
        assert i["metadata"]["filename"] == example_filename
    assert len("".join(elem["text"] for elem in response.json())) > 20

    # Just hit the second path (posting multiple files) to bump the coverage
    # We'll come back and make smarter tests
    response = client.post(
        MAIN_API_ROUTE,
        files=[
            ("files", (str(test_file), open(test_file, "rb"), content_type)),
            ("files", (str(test_file), open(test_file, "rb"), content_type)),
        ],
    )
    assert response.status_code == 200
    assert all(x["metadata"]["filename"] == example_filename for i in response.json() for x in i)

    assert len(response.json()) > 0

    csv_response = client.post(
        MAIN_API_ROUTE,
        files=[
            ("files", (str(test_file), open(test_file, "rb"), content_type)),
            ("files", (str(test_file), open(test_file, "rb"), content_type)),
        ],
        data={"output_format": "text/csv"},
    )
    assert csv_response.status_code == 200
    dfs = pd.read_csv(io.StringIO(csv_response.text))
    assert len(dfs) > 0


def test_metadata_fields_removed():
    """
    Verify that responses do not include coordinates unless requested
    Verify that certain other metadata fields are dropped
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.jpg"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"strategy": "hi_res"},
    )

    assert response.status_code == 200
    response_without_coords = response.json()

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"coordinates": "true", "strategy": "hi_res"},
    )

    assert response.status_code == 200
    response_with_coords = response.json()

    # Each element should be the same except for the coordinates field
    # Also, check for metadata fields we explicitly dropped
    for i in range(len(response_with_coords)):
        assert "coordinates" in response_with_coords[i]["metadata"]
        del response_with_coords[i]["metadata"]["coordinates"]
        assert response_with_coords[i] == response_without_coords[i]

        assert "last_modified" not in response_without_coords[i]["metadata"]
        assert "file_directory" not in response_without_coords[i]["metadata"]
        assert "detection_class_prob" not in response_without_coords[i]["metadata"]


def test_ocr_languages_param():  # will eventually be depricated
    """
    Verify that we get the corresponding languages from the response with ocr_languages
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "english-and-korean.png"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"strategy": "ocr_only", "ocr_languages": ["eng", "kor"]},
    )

    assert response.status_code == 200
    elements = response.json()
    assert elements[3]["text"].startswith("안녕하세요, 저 희 는 YGEAS 그룹")


def test_languages_param():
    """
    Verify that we get the corresponding languages from the response with `languages`
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "english-and-korean.png"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"strategy": "ocr_only", "languages": ["eng", "kor"]},
    )

    assert response.status_code == 200
    elements = response.json()
    assert elements[3]["text"].startswith("안녕하세요, 저 희 는 YGEAS 그룹")


def test_languages_and_ocr_languages_raises_error():
    """
    Verify that we get the corresponding languages from the response with `languages`
    """
    with pytest.raises(ValueError):
        client = TestClient(app)
        test_file = Path("sample-docs") / "english-and-korean.png"
        client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(test_file), open(test_file, "rb")))],
            data={
                "strategy": "ocr_only",
                "languages": ["eng", "kor"],
                "ocr_languages": ["eng", "kor"],
            },
        )


def test_skip_infer_table_types_param():
    """
    Verify that we skip table instruction unless specified
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-with-table.jpg"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
    )

    assert response.status_code == 200
    # test we skip table extraction by default
    elements = response.json()
    table = [el["metadata"]["text_as_html"] for el in elements if "text_as_html" in el["metadata"]]
    assert len(table) == 0

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"skip_infer_table_types": "['pdf']"},
    )

    assert response.status_code == 200
    # test we didn't specify to skip table extration with image
    elements = response.json()
    table = [el["metadata"]["text_as_html"] for el in elements if "text_as_html" in el["metadata"]]
    assert len(table) == 1
    # This text is not currently picked up
    # assert "Layouts of history Japanese documents" in table[0]


def test_strategy_param_400():
    """Verify that we get a 400 if we pass in a bad strategy"""
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper.pdf"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "text/plain"))],
        data={"strategy": "not_a_strategy"},
    )
    assert response.status_code == 422


def test_valid_encoding_param():
    """
    Verify that we get a 200 for passing an encoding param
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-xml.xml"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "text/plain"))],
        data={"encoding": "ascii"},
    )
    assert response.status_code == 200


def test_invalid_encoding_param():
    """
    Verify that we get a 500 if we pass an invalid encoding through to partition
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-html.html"
    with pytest.raises(LookupError) as excinfo:
        client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(test_file), open(test_file, "rb"), "text/plain"))],
            data={"encoding": "not_an_encoding"},
        )
    assert "unknown encoding" in str(excinfo.value)


def test_api_with_different_encodings():
    """
    Verify that we get different text results for different encodings
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-text-utf-32.txt"

    # utf-16
    response_16 = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "text/plain"))],
        data={"encoding": "utf-16"},
    )
    assert response_16.status_code == 200
    elements_16 = response_16.json()
    assert elements_16[0]["text"].startswith("\x00T\x00h\x00i\x00s\x00 \x00i\x00s\x00")

    # utf-32
    response_32 = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "text/plain"))],
        data={"encoding": "utf-32"},
    )
    assert response_32.status_code == 200
    elements_32 = response_32.json()
    assert elements_32[2]["text"].startswith("Important points:")

    # utf-8
    with pytest.raises(UnicodeDecodeError) as excinfo:
        client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(test_file), open(test_file, "rb"), "text/plain"))],
            data={"encoding": "utf8"},
        )
    assert "invalid start byte" in str(excinfo.value)


def test_xml_keep_tags_param():
    """
    Verify that responses do not include xml tags unless requested
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-xml.xml"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"strategy": "hi_res"},
    )
    assert response.status_code == 200
    response_without_xml_tags = response.json()

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"xml_keep_tags": "true", "strategy": "hi_res"},
    )
    assert response.status_code == 200

    # xml_keep_tags returns one element with the full xml
    # Just assert the tags are still present
    response_with_xml_tags = response.json()[0]
    for element in response_without_xml_tags:
        assert element["text"].replace("&", "&amp;") in response_with_xml_tags["text"]


def test_include_page_breaks_param():
    """
    Verify that responses do not include page breaks unless requested
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"strategy": "fast"},
    )
    assert response.status_code == 200
    response_without_page_breaks = response.json()

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"include_page_breaks": "true", "strategy": "fast"},
    )
    assert response.status_code == 200
    response_with_page_breaks = response.json()

    # The responses should have the same content except extra PageBreak objects
    response_with_page_breaks_index, response_without_page_breaks_index = 0, 0
    while response_with_page_breaks_index <= len(response_without_page_breaks):
        curr_response_with_page_breaks_element = response_with_page_breaks[
            response_with_page_breaks_index
        ]
        curr_response_without_page_breaks_element = response_without_page_breaks[
            response_without_page_breaks_index
        ]
        if curr_response_with_page_breaks_element["type"] == "PageBreak":
            assert curr_response_without_page_breaks_element["type"] != "PageBreak"

            response_with_page_breaks_index += 1
        else:
            assert (
                curr_response_without_page_breaks_element["text"]
                == curr_response_with_page_breaks_element["text"]
            )

            response_with_page_breaks_index += 1
            response_without_page_breaks_index += 1

    last_response_with_page_breaks_element = response_with_page_breaks[
        response_with_page_breaks_index
    ]
    assert last_response_with_page_breaks_element["type"] == "PageBreak"
    assert response_without_page_breaks[-1]["type"] != "PageBreak"


def test_include_extract_image_block_types_param():
    """
    Verify that responses do not include base64 image in Table/Image metadata unless requested.
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "embedded-images-tables.pdf"
    with open(test_file, "rb") as file:
        response = client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(test_file), file))],
            data={"strategy": "hi_res"},
        )

    assert response.status_code == 200
    response_without_image = response.json()

    with open(test_file, "rb") as file:
        response = client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(test_file), file))],
            data={"strategy": "hi_res", "extract_image_block_types": '["Image", "Table"]'},
        )

    assert response.status_code == 200
    response_with_image = response.json()

    # Each element should be the same except for the image_base64 and image_mime_type fields
    # in metadata
    assert len(response_without_image) == len(response_with_image)
    for element, element_with_image in zip(response_without_image, response_with_image):
        if element["type"] in ["Image", "Table"]:
            assert "image_base64" in element_with_image["metadata"]
            assert "image_mime_type" in element_with_image["metadata"]
            del element_with_image["metadata"]["image_base64"]
            del element_with_image["metadata"]["image_mime_type"]
            assert element == element_with_image


@pytest.mark.parametrize(
    "example_filename",
    [
        "fake-xml.xml",
    ],
)
def test_general_api_returns_400_unsupported_file(example_filename):
    client = TestClient(app)
    test_file = Path("sample-docs") / example_filename
    filetype = "invalid/filetype"
    response = client.post(
        MAIN_API_ROUTE, files=[("files", (str(test_file), open(test_file, "rb"), filetype))]
    )
    assert response.json() == {"detail": f"File type {filetype} is not supported."}
    assert response.status_code == 400


def test_general_api_returns_422_bad_pdf():
    """
    Verify that we get a 422 for invalid PDF files
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf")
    tmp.write(b"This is not a valid PDF")
    client = TestClient(app)
    response = client.post(
        MAIN_API_ROUTE, files=[("files", (str(tmp.name), open(tmp.name, "rb"), "application/pdf"))]
    )
    assert response.json() == {"detail": "File does not appear to be a valid PDF"}
    assert response.status_code == 422
    tmp.close()

    # Don't blow up if this isn't actually a pdf
    test_file = Path("sample-docs") / "fake-power-point.pptx"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
    )

    assert response.json() == {"detail": "File does not appear to be a valid PDF"}
    assert response.status_code == 422


def test_general_api_returns_503(monkeypatch):
    """
    When available memory is below the minimum. return a 503, unless our origin ip is 10.{4,5}.x.x
    """
    monkeypatch.setenv("UNSTRUCTURED_MEMORY_FREE_MINIMUM_MB", "300000")

    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-xml.xml"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
    )

    assert response.status_code == 503


def test_general_api_returns_401(monkeypatch):
    """
    When UNSTRUCTURED_API_KEY is set, return a 401 if the unstructured-api-key header does not match
    """
    monkeypatch.setenv("UNSTRUCTURED_API_KEY", "foobar")

    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-xml.xml"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        headers={"unstructured-api-key": "foobar"},
    )

    assert response.status_code == 200

    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-xml.xml"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        headers={"unstructured-api-key": "helloworld"},
    )

    assert response.status_code == 401


class MockResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.body = {}
        self.text = ""

    def json(self):
        return self.body


def test_parallel_mode_passes_params(monkeypatch):
    """
    Verify that parallel mode passes all params correctly into local partition.
    If you add something to partition_kwargs, you need to explicitly test it here
    with some non default value.
    TODO - do the same test when params are sent back to the api
    """
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_ENABLED", "true")

    # Make this really big so we just call partition
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_SPLIT_SIZE", "500")

    mock_partition = Mock(return_value={})

    monkeypatch.setattr(
        general,
        "partition",
        mock_partition,
    )

    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper.pdf"

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
        data={
            "encoding": "foo",
            "hi_res_model_name": "yolox",
            "include_page_breaks": "True",
            "languages": "foo",
            "pdf_infer_table_structure": "True",
            "strategy": "hi_res",
            "xml_keep_tags": "True",
            "skip_infer_table_types": "foo",
            # -- chunking options --
            "chunking_strategy": "by_title",
            "combine_under_n_chars": "501",
            "max_characters": "1502",
            "multipage_sections": "False",
            "new_after_n_chars": "1501",
            "overlap": "25",
            "overlap_all": "true",
        },
    )

    assert response.status_code == 200

    mock_partition.assert_called_once_with(
        file=ANY,
        metadata_filename=str(test_file),
        content_type="application/pdf",
        hi_res_model_name="yolox",
        encoding="foo",
        include_page_breaks=True,
        ocr_languages=None,
        languages=["foo"],
        pdf_infer_table_structure=True,
        strategy="hi_res",
        xml_keep_tags=True,
        skip_infer_table_types="foo",
        extract_image_block_types=None,
        extract_image_block_to_payload=False,
        # -- chunking options --
        chunking_strategy="by_title",
        combine_text_under_n_chars=501,
        max_characters=1502,
        multipage_sections=False,
        new_after_n_chars=1501,
        overlap=25,
        overlap_all=True,
    )


def test_parallel_mode_returns_errors(monkeypatch):
    """
    If we get an error sending a page to the api, bubble it up
    """
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_ENABLED", "true")
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_URL", "unused")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: MockResponse(status_code=500),
    )

    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper.pdf"

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
    )

    assert response.status_code == 500

    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: MockResponse(status_code=400),
    )

    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper.pdf"

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
    )

    assert response.status_code == 400


def test_partition_file_via_api_will_retry(monkeypatch, mocker):
    """
    Verify number of retries with parallel mode
    """
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_ENABLED", "true")
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_URL", "unused")
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_THREADS", "1")

    num_calls = 0

    # Validate the retry count by returning an error the first 2 times
    def mock_response(*args, **kwargs):
        nonlocal num_calls
        num_calls += 1

        if num_calls <= 2:
            return MockResponse(status_code=500)

        return MockResponse(status_code=200)

    monkeypatch.setattr(
        requests,
        "post",
        mock_response,
    )

    # This needs to be mocked when we return 200
    mocker.patch("prepline_general.api.general.elements_from_json")

    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
    )

    assert response.status_code == 200


def test_partition_file_via_api_not_retryable_error_code(monkeypatch, mocker):
    """
    Verify we didn't retry if the error code is not retryable
    """
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_ENABLED", "true")
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_URL", "unused")
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_THREADS", "1")
    monkeypatch.setenv("UNSTRUCTURED_PARALLEL_MODE_RETRY_ATTEMPTS", "3")

    remote_partition = Mock(side_effect=HTTPException(status_code=401))

    monkeypatch.setattr(
        requests,
        "post",
        remote_partition,
    )
    client = TestClient(app)
    test_file = Path("sample-docs") / "list-item-example.pdf"

    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
    )

    assert response.status_code == 401

    assert remote_partition.called_once()


def test_chunking_strategy_param():
    """
    Verify that responses do not chunk elements unless requested
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"strategy": "hi_res"},
    )
    assert response.status_code == 200
    response_without_chunking = response.json()

    # chunking
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"chunking_strategy": "by_title"},
    )
    assert response.status_code == 200

    response_with_chunking = response.json()
    assert len(response_with_chunking) != len(response_without_chunking)
    assert "CompositeElement" in [element.get("type") for element in response_with_chunking]


# Defaults:
# multippage = True, combine_text_under_n_chars = None, new_after_n_chars = None,
# max_characters = 500
@pytest.mark.parametrize(
    ("multipage_sections", "combine_under_n_chars", "new_after_n_chars", "max_characters"),
    [
        (False, None, None, 500),  # test multipage_sections
        (True, 1000, None, 5000),  # test combine_under_n_chars
        (True, None, 10, 500),  # test new_after_n_chars
        (True, None, None, 100),  # test max__characters
    ],
)
def test_chunking_strategy_additional_params(
    multipage_sections, combine_under_n_chars, new_after_n_chars, max_characters
):
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"

    arg_resp = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={
            "chunking_strategy": "by_title",
            "multipage_sections": multipage_sections,
            "combine_under_n_chars": combine_under_n_chars,
            "new_after_n_chars": new_after_n_chars,
            "max_characters": max_characters,
        },
    )
    arg_resp_json = arg_resp.json()

    default_resp = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"chunking_strategy": "by_title"},
    )
    default_resp_json = default_resp.json()

    assert arg_resp_json != default_resp_json


def test_encrypted_pdf():
    """
    Test that we throw an error if a pdf is password protected.
    A pdf can be encrypted but still readable - don't throw an error here.
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"
    original_pdf = PdfReader(test_file)

    with tempfile.NamedTemporaryFile() as temp_file:
        # This file is user encrypted and cannot be read
        writer = PdfWriter()
        writer.append_pages_from_reader(original_pdf)
        writer.encrypt(user_password="password123")
        writer.write(temp_file.name)

        # Response should be 400
        response = client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(temp_file.name), open(temp_file.name, "rb"), "application/pdf"))],
        )
        assert response.json() == {"detail": "File is encrypted. Please decrypt it with password."}
        assert response.status_code == 400

        # This file is owner encrypted, i.e. readable with edit restrictions
        writer = PdfWriter()
        writer.append_pages_from_reader(original_pdf)
        writer.encrypt(user_password="", owner_password="password123", permissions_flag=0b1100)
        writer.write(temp_file.name)

        # Response should be 200
        response = client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(temp_file.name), open(temp_file.name, "rb"), "application/pdf"))],
        )
        assert response.status_code == 200


def test_general_api_returns_422_bad_docx():
    """
    Verify that we get a 400 for invalid docx files
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "fake-text.txt"
    response = client.post(
        MAIN_API_ROUTE,
        files=[
            (
                "files",
                (
                    str(test_file),
                    open(test_file, "rb"),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ),
            )
        ],
    )
    assert response.json().get("detail") == "File is not a valid docx"
    assert response.status_code == 422


def test_general_api_returns_400_bad_json(tmpdir):
    """
    Verify that we get a 400 for invalid json schemas
    """
    client = TestClient(app)
    data = '{"hi": "there"}'

    filepath = os.path.join(tmpdir, "unprocessable.json")
    with open(filepath, "w") as f:
        f.write(data)
    response = client.post(
        MAIN_API_ROUTE,
        files=[
            (
                "files",
                (
                    str(filepath),
                    open(filepath, "rb"),
                ),
            )
        ],
    )
    assert "Unstructured schema" in response.json().get("detail")
    assert response.status_code == 400


def test_chipper_memory_protection(monkeypatch, mocker):
    """
    For now, only 1 Chipper call is allowed at a time.
    Assert that we return a 503 while it's in use.
    """

    def mock_partition(*args, **kwargs):
        time.sleep(2)
        return {}

    monkeypatch.setattr(
        general,
        "partition",
        mock_partition,
    )

    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"

    def make_request(*args):
        return client.post(
            MAIN_API_ROUTE,
            files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
            data={"strategy": "hi_res", "hi_res_model_name": "chipper"},
        )

    with ThreadPoolExecutor() as executor:
        responses = list(executor.map(make_request, range(3)))

        status_codes = [response.status_code for response in responses]

        # Assert only one call got through
        assert status_codes.count(200) == 1
        assert status_codes.count(503) == 2


def test_invalid_strategy_for_image_file():
    """
    Verify that we get a 400 error if we use "strategy=fast" with an image file
    """
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.jpg"
    resp = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={"strategy": "fast"},
    )
    assert resp.status_code == 400
    assert "fast strategy is not available for image files" in resp.text


@pytest.mark.parametrize(
    ("exception", "status_code", "message"),
    [
        (
            OSError("chipper-fast-fine-tuning is not a local folder"),
            400,
            "The Chipper model is not available for download. "
            "It can be accessed via the official hosted API.",
        ),
        (
            OSError("ved-fine-tuning is not a local folder"),
            400,
            "The Chipper model is not available for download. "
            "It can be accessed via the official hosted API.",
        ),
        (OSError(1, "An error happened"), 500, "[Errno 1] An error happened"),
    ],
)
def test_chipper_not_available_errors(monkeypatch, mocker, exception, status_code, message):
    """
    Assert that we return the right error if Chipper is not downloaded.
    OSError can have an int as the first arg, do not blow up if that happens.
    """

    mock_partition = Mock(side_effect=exception)

    monkeypatch.setattr(
        general,
        "partition",
        mock_partition,
    )

    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"

    resp = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb"), "application/pdf"))],
        data={"strategy": "hi_res", "hi_res_model_name": "chipper"},
    )

    assert resp.status_code == status_code
    assert resp.json().get("detail") == message


def test_invalid_hi_res_model_name_returns_400():
    """Verify that we get a 400 if we pass in a bad model_name"""
    client = TestClient(app)
    test_file = Path("sample-docs") / "layout-parser-paper-fast.pdf"
    response = client.post(
        MAIN_API_ROUTE,
        files=[("files", (str(test_file), open(test_file, "rb")))],
        data={
            "strategy": "hi_res",
            "hi_res_model_name": "invalid_model",
        },
    )
    assert response.status_code == 400
    assert "Unknown model type" in response.text


def test_get_request():
    client = TestClient(app)
    response = client.get("/general/v0/general")
    assert response.status_code == 405
    assert response.json() == {"detail": "Only POST requests are supported."}
