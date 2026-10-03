"""브라우저에 직접 보여 주는 안내 화면. 고정 문구만 담아 사용자 입력이 섞이지 않는다."""

from fastapi.responses import HTMLResponse

_PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{
  font-family: system-ui, sans-serif;
  display: grid;
  place-items: center;
  min-height: 100vh;
  margin: 0;
}}
main {{ max-width: 28rem; padding: 2rem; text-align: center; }}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
<p>{message}</p>
</main>
</body>
</html>
"""


def _page(title: str, message: str) -> HTMLResponse:
    # 승인 결과 화면이 브라우저·중간 프록시에 남지 않게 한다.
    return HTMLResponse(
        _PAGE.format(title=title, message=message), headers={"Cache-Control": "no-store"}
    )


def cli_login_done_page() -> HTMLResponse:
    return _page("로그인이 완료되었습니다", "터미널로 돌아가세요. 이 창은 닫아도 됩니다.")


def cli_login_cancelled_page() -> HTMLResponse:
    return _page(
        "로그인이 취소되었습니다",
        "터미널에서 <code>likelion login</code> 을 다시 실행해 주세요.",
    )
