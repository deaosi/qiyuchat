"""Read files from a single Feishu ticket feedback attachment column."""

from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic
from typing import TypedDict

from deploy.feishu_sync import ticket_fields
from src import voc_feishu_sync as sync
from src.qiyu.ticket import MAX_REPLY_ATTACHMENTS, MAX_REPLY_ATTACHMENT_BYTES


_FILE_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}")
_DOWNLOAD_TIMEOUT = 20


class Attachment(TypedDict):
    name: str
    file_token: str
    size: int


def parse_cell(value) -> list[Attachment]:
    """Return a stable revision from native Base media, independent of transient URLs."""
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise sync.SyncError("技术回复附件须填写飞书附件列。", 409)
    files = {}
    for item in value:
        if not isinstance(item, dict):
            raise sync.SyncError("技术回复附件资料不完整，请重新上传。", 409)
        name, token, size = (item.get(key) for key in ("name", "file_token", "size"))
        if (not isinstance(name, str) or not name.strip() or len(name) > 128 or name in (".", "..")
                or not name.isprintable() or "/" in name or "\\" in name
                or not isinstance(token, str) or not _FILE_TOKEN.fullmatch(token)
                or type(size) is not int or size <= 0):
            raise sync.SyncError("技术回复附件名称或资料无效，请检查文件后重新上传。", 409)
        attachment = Attachment(name=name, file_token=token, size=size)
        if token in files and files[token] != attachment:
            raise sync.SyncError("飞书返回的技术回复附件资料不一致，请重新上传。", 409)
        files[token] = attachment
    return [files[token] for token in sorted(files)]


def _download_file(target, record_id, field_id, file, folder, output, deadline) -> bytes:
    # The Base shortcut resolves the token on this record and supplies its attachment permission context.
    args = [target.cli, "base", "+record-download-attachment", "--base-token", target.base_token,
            "--table-id", target.table_id, "--record-id", record_id, "--file-token", file["file_token"],
            "--output", output, "--as", "bot", "--format", "json"]
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise sync.SyncError("下载技术回复附件超时，稍后重试。", 504)
    try:
        result = subprocess.run(args, cwd=folder, env=sync._cli_environment(), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=remaining, check=False)
    except subprocess.TimeoutExpired:
        raise sync.SyncError("下载技术回复附件超时，稍后重试。", 504) from None
    except OSError:
        raise sync.SyncError("飞书 CLI 暂时无法下载技术回复附件。", 503) from None
    response = sync._parse_json(result.stdout)
    if result.returncode or response.get("ok") is not True or response.get("identity") != "bot":
        error = sync._parse_json(result.stderr) or response
        problem = error.get("error")
        message = problem.get("message", "") if isinstance(problem, dict) else ""
        if sync._cli_error(error).status == 403 or isinstance(message, str) and "HTTP 403" in message:
            raise sync.SyncError("飞书应用无权下载技术回复附件，请核对云文档素材下载权限。", 403)
        raise sync.SyncError("飞书技术回复附件下载失败，请核对附件后重试。")
    data = response.get("data")
    files = data.get("downloaded") if isinstance(data, dict) else None
    downloaded = files[0] if isinstance(files, list) and len(files) == 1 else None
    if (not isinstance(downloaded, dict) or downloaded.get("record_id") != record_id
            or downloaded.get("field_id") != field_id or downloaded.get("file_token") != file["file_token"]
            or downloaded.get("name") != file["name"] or downloaded.get("size_bytes") != file["size"]):
        raise sync.SyncError("飞书技术回复附件已变动，请重新同步当前记录。", 409)
    try:
        with (Path(folder) / output).open("rb") as stream:
            content = stream.read(file["size"] + 1)
    except OSError:
        raise sync.SyncError("无法读取下载的飞书技术回复附件。", 503) from None
    if len(content) != file["size"]:
        raise sync.SyncError("飞书技术回复附件未完整下载，请重试。")
    return content


def download(target, record_id: str, field_id: str, files: list[Attachment]) -> list[tuple[str, bytes]]:
    """Read original bytes without persisting a media URL or file."""
    if not sync._RECORD_ID.fullmatch(record_id) or not ticket_fields._FIELD_ID.fullmatch(field_id):
        raise sync.SyncError("无法核对飞书技术回复附件所属的记录或字段。", 409)
    files = parse_cell(files)
    if len(files) > MAX_REPLY_ATTACHMENTS:
        raise sync.SyncError(f"每次技术回复最多回传 {MAX_REPLY_ATTACHMENTS} 个附件。", 409)
    if sum(item["size"] for item in files) > MAX_REPLY_ATTACHMENT_BYTES:
        raise sync.SyncError("每次技术回复回传的附件合计不能超过 5 MB。", 409)
    if not files:
        return []
    deadline = monotonic() + _DOWNLOAD_TIMEOUT
    with TemporaryDirectory(prefix="yuhai-ticket-attachments-") as folder:
        return [(file["name"], _download_file(target, record_id, field_id, file, folder,
                                              f"attachment-{index}.bin", deadline))
                for index, file in enumerate(files)]
