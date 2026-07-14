import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright


AUTH_STATE_PATH = Path(os.getenv("THREADS_AUTH_STATE_PATH", "storage/threads_auth_state.json"))


async def main() -> int:
    AUTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    wait_seconds = int(os.getenv("THREADS_LOGIN_WAIT_SECONDS", "300"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            locale="zh-TW",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        )
        page = await context.new_page()
        await page.goto("https://www.threads.com/", wait_until="domcontentloaded", timeout=60000)
        print(f"Please log in to Threads in the browser window. Waiting up to {wait_seconds} seconds...")
        logged_in = False
        for _ in range(max(1, wait_seconds // 5)):
            await page.wait_for_timeout(5000)
            state = await page.evaluate(
                """
                () => {
                  const body = (document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
                  const hasLoginLink = !!document.querySelector('a[href*="/login"]');
                  const hasComposer = body.includes('新串文') || body.includes('開始串文') || body.includes('發佈');
                  const loggedOutText = body.includes('登入或註冊')
                    || body.includes('使用 Instagram 帳號繼續')
                    || body.includes('使用 Instagram 帳號登入')
                    || body.includes('忘記密碼')
                    || body.includes('密碼錯誤');
                  return { hasLoginLink, hasComposer, loggedOutText, body: body.slice(0, 300) };
                }
                """
            )
            if state["hasComposer"] or (not state["hasLoginLink"] and not state["loggedOutText"]):
                logged_in = True
                break
            print(f"Still waiting for login... current page text: {state['body']}")

        if not logged_in:
            await browser.close()
            print("Threads login was not detected. Please run this script again and finish login in the browser window.")
            return 1

        await context.storage_state(path=str(AUTH_STATE_PATH))
        await browser.close()

    print(f"Saved Threads auth state to {AUTH_STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
