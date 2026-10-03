"""업로드를 소스로 쓰는 배포 요청의 `source_sha` 표기.

Git 커밋 SHA 는 16진수뿐이라 접두어 `upload-` 가 붙은 값과 겹칠 수 없다. 그래서 이 값만 보고
GitHub 에서 받을 수 있는 소스인지 가린다(재배포 거절, 진단의 커밋 SHA 생략).
"""

UPLOAD_SOURCE_SHA_PREFIX = "upload-"
# 아카이브 sha256 의 앞 몇 자를 지문으로 쓴다. 같은 내용을 올리면 같은 값이다.
_FINGERPRINT_LENGTH = 12


def build_upload_source_sha(archive_sha256: str) -> str:
    return f"{UPLOAD_SOURCE_SHA_PREFIX}{archive_sha256[:_FINGERPRINT_LENGTH]}"


def is_upload_source_sha(source_sha: str) -> bool:
    return source_sha.startswith(UPLOAD_SOURCE_SHA_PREFIX)
