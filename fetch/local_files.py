"""本地文件（PDF / Office）原件导入：来源 local_files 的导入插件（唯一采集框架的一部分）。

旧 ETL 的 /api/documents/upload 已删除；本地原件现在走和站点附件同一条链：
1. 原件按 sha256 固定落盘（fetch.web_client.store_original，与远程附件 download_pdf 同一写法：
   临时文件 fsync → 同目录硬链接不覆盖 → 同步祖先目录），路径 <RAW_DIR>/local_files/<uuid5(记录)>.<sha256><后缀>；
2. 生成一条附件版本（fetch.attachments，role=attachment，记录身份 local-file:<集合>/<文件名>）；
3. 发布进只追加的 history 版本（上传：store.history.import_archive；收件目录：/fetch 读取任务），
4. 正文由加工层 clean.derived_text → clean.attachment_parse（自部署 MinerU 等）按版本产出，带页/块/表定位。

两个入口：
- POST /api/history/local-files（multipart，管理员凭据）：单文件，大小上限见 max_upload_bytes()；
- POST /api/history/fetch {"source_id":"local_files","query":"inbox","source_record_ids":[收件目录内相对路径]}：
  读服务器收件目录 DATAFABRIC_LOCAL_IMPORT_DIR 里的文件（不跟随出目录的路径）。
类型白名单：加工层能解析的 PDF / DOCX / DOC / XLSX / XLS / PPTX / PPT，扩展名和文件头必须一致。
许可：上传者负责，系统不核实，如实记在 facts.access。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path, PurePosixPath

from fetch.attachments import attachment_record, local_record_id
from store.history_models import ArchiveImport, HistoryError

SOURCE_ID = "local_files"
ROUTE_BUDGET = 15 * 1024 * 1024  # /api/history 的 POST 整体预算是 16MiB，multipart 头另占一点
PK, OLE = b"PK\x03\x04", b"\xd0\xcf\x11\xe0"
# 扩展名 → (文件头, zip 内必须有的目录)
SIGNATURES = {
    ".pdf": (b"%PDF-", None),
    ".docx": (PK, "word/"),
    ".xlsx": (PK, "xl/"),
    ".pptx": (PK, "ppt/"),
    ".doc": (OLE, None),
    ".xls": (OLE, None),
    ".ppt": (OLE, None),
}
ACCESS = {
    "tier": "uploaded_original",
    "license": None,
    "basis": "管理员上传或放入收件目录的原件；许可与来源由上传者负责，系统未核实",
}


def max_upload_bytes() -> int:
    value = int(os.environ.get("DATAFABRIC_LOCAL_FILE_MAX_BYTES", str(ROUTE_BUDGET)))
    return max(1, min(value, ROUTE_BUDGET))


def max_inbox_file_bytes() -> int:
    return int(os.environ.get("DATAFABRIC_ATTACHMENT_MAX_BYTES", str(100 * 1024 * 1024)))


def clean_collection(value: str) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"\w[\w.:-]{0,119}", value):
        raise HistoryError("invalid_collection", "集合名须为 1-120 个字母/数字/汉字/._:-，不含路径分隔符")
    return value


def _clean_part(part: str) -> str:
    if (not part or part in {".", ".."} or len(part) > 200 or part != part.strip()
            or any(ord(c) < 32 or c in "\\/:*?\"<>|" for c in part)):
        raise HistoryError("invalid_file_name", "文件名不合法")
    return part


def clean_name(filename: str) -> str:
    """上传文件名只取最后一段（浏览器可能带客户端路径）。"""
    name = re.split(r"[\\/]", (filename or "").strip())[-1]
    return _clean_part(name)


def clean_relative(value: str) -> str:
    """收件目录内的相对路径；绝对路径、.. 一律拒绝。"""
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        raise HistoryError("invalid_file_name", "须为收件目录内的相对路径")
    parts = PurePosixPath(value).parts
    if len(parts) > 8:
        raise HistoryError("invalid_file_name", "目录层级过深")
    return "/".join(_clean_part(p) for p in parts)


def file_type(name: str, content: bytes) -> str:
    """扩展名在白名单里且与文件头一致，返回扩展名；否则 415。"""
    ext = PurePosixPath(name).suffix.lower()
    if ext not in SIGNATURES:
        raise HistoryError("unsupported_media", "只接受 " + "/".join(sorted(SIGNATURES)) + " 文件", 415)
    magic, member = SIGNATURES[ext]
    ok = content.startswith(magic)
    if ok and member:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                ok = any(n.startswith(member) for n in archive.namelist())
        except zipfile.BadZipFile:
            ok = False
    if not ok:
        raise HistoryError("media_mismatch", "文件内容与扩展名 " + ext + " 不符", 415)
    return ext


def prepare(content: bytes, collection: str, name: str, *, discovered_from: str):
    """校验类型 → 原件按 sha256 落盘 → 附件版本原件。name 是集合内文件名（收件目录为相对路径）。"""
    if not content:
        raise HistoryError("empty_file", "文件为空")
    ext = file_type(name, content)
    record_id = local_record_id(collection, name)
    from fetch.web_client import store_original

    stored = store_original(content, record_id, SOURCE_ID, ext)
    return attachment_record(
        SOURCE_ID,
        record_id=record_id,
        stored=stored,
        file_name=PurePosixPath(name).name,
        file_ext=ext,
        discovered_from=discovered_from,
        access=dict(ACCESS),
        extra={"collection": collection, "collection_name": name},
    )


def import_upload(content: bytes, filename: str, collection: str, is_demo: bool, key: str):
    """上传入口：一个文件 → 一条附件版本，经 import_archive 发布（幂等键同 /archives）。"""
    from store import history as store

    if len(content) > max_upload_bytes():
        raise HistoryError("file_too_large", "文件超过上传上限", 413)
    collection, name = clean_collection(collection), clean_name(filename)
    file_type(name, content)  # 先拒类型，不为被拒的文件落盘
    if SOURCE_ID not in store.allowed_sources():
        raise HistoryError("source_not_allowed", "当前许可不允许该来源", 403)
    record = prepare(content, collection, name, discovered_from="upload")
    request = ArchiveImport(
        source_id=SOURCE_ID,
        is_demo=is_demo,
        scope_description="本地文件导入：" + collection + "/" + name,
        declared_count=1,
        records=[record],
    )
    return store.import_archive(request, key)


def _inbox() -> Path:
    value = os.environ.get("DATAFABRIC_LOCAL_IMPORT_DIR", "")
    if not value:
        raise HistoryError("local_import_not_configured", "本地收件目录未配置", 503)
    return Path(value).resolve()


def read_local(request) -> dict:
    """导入插件 local_file_import：读收件目录里明确列出的文件。返回值形状与 read_public 其他插件相同。
    版本数和截止时间在循环里数（fetch.transport.Budget）；单个文件不超过 max_inbox_file_bytes。"""
    from fetch import transport

    records, coverage = [], []
    stop, error = "upstream_exhausted", None
    budget = transport.Budget(request)
    try:
        if request.query != "inbox":
            raise HistoryError("unknown_query", "local_files 只支持 query=inbox")
        if not request.source_record_ids:
            raise HistoryError("ids_required", "须列出收件目录内的相对路径")
        root = _inbox()
        names = [clean_relative(value) for value in request.source_record_ids]
        for position, name in enumerate(names):
            path = (root / name).resolve()
            entry = {"record_id": local_record_id("inbox", name), "path": name}
            if root not in path.parents or not path.is_file():
                coverage.append({**entry, "status": "missing"})
                continue
            if path.stat().st_size > max_inbox_file_bytes():
                coverage.append({**entry, "status": "too_large"})
                continue
            try:
                budget.need_record()
            except transport.Stop as stopped:
                stop = stopped.reason
                coverage.extend({"record_id": local_record_id("inbox", later), "path": later,
                                 "status": "not_attempted"} for later in names[position:])
                break
            try:
                record = prepare(path.read_bytes(), "inbox", name, discovered_from="local_inbox")
            except HistoryError as exc:
                coverage.append({**entry, "status": exc.code})
                continue
            budget.take_record()
            records.append(record)
            coverage.append({**entry, "status": "stored"})
    except HistoryError as exc:
        error, stop = {"code": exc.code, "message": exc.message}, "source_failed"
    return {
        "records": records,
        "complete": False,
        "upstream_pagination_exhausted": stop == "upstream_exhausted",
        "stop_reason": stop,
        "error": error,
        "source_total": len(request.source_record_ids),
        "count_drift": None,
        "next_continuation": None,
        "attachment_coverage": coverage,
    }
