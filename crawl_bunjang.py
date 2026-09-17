# pip install playwright requests
# playwright install chromium

import asyncio, re, time, os, sys, hashlib
from playwright.async_api import async_playwright
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# AI 생성 사기 유인이 큰 고가/희소 아이템 위주 키워드
KEYWORDS = [
    "나이키 덩크", "조던1 로우", "조던1 하이", "루이비통 가방", "샤넬 지갑", "롤렉스",
    "아이폰 프로", "맥북 프로", "그래픽카드", "카메라 렌즈", "포토카드 시그",
    "닌텐도 스위치", "플레이스테이션5",
]

# 목표 개수 미달 시 세부 검색어로 쪼개서 추가 수집할 때 사용
SUB_KEYWORDS = {
    "나이키 덩크": ["나이키 덩크 로우 판다", "나이키 덩크 로우 UNC", "나이키 덩크 하이 시카고", "나이키 덩크 로우 그레이포그"],
    "조던1 로우": ["조던1 로우 트래비스", "조던1 로우 판다", "조던1 로우 스타피쉬", "조던1 로우 울프그레이"],
    "조던1 하이": ["조던1 하이 시카고", "조던1 하이 브레드", "조던1 하이 유니버시티블루", "조던1 하이 다크모카"],
    "루이비통 가방": ["루이비통 스피디", "루이비통 네버풀", "루이비통 알마", "루이비통 온더고"],
    "샤넬 지갑": ["샤넬 클래식 지갑", "샤넬 반지갑", "샤넬 장지갑", "샤넬 카드지갑"],
    "롤렉스": ["롤렉스 서브마리너", "롤렉스 데이토나", "롤렉스 데이트저스트", "롤렉스 익스플로러"],
    "아이폰 프로": ["아이폰15 프로", "아이폰14 프로", "아이폰13 프로", "아이폰 프로맥스"],
    "맥북 프로": ["맥북 프로 M1", "맥북 프로 M2", "맥북 프로 M3", "맥북 프로 16인치"],
    "그래픽카드": ["RTX4090", "RTX4080", "RTX3080", "RTX3090"],
    "카메라 렌즈": ["캐논 렌즈", "소니 렌즈", "니콘 렌즈", "삼양 렌즈"],
    "포토카드 시그": ["포토카드 시그 미개봉", "포토카드 시그 양도", "포토카드 시그 급처", "포토카드 시그 컬렉"],
    "닌텐도 스위치": ["닌텐도 스위치 라이트", "닌텐도 스위치 OLED", "닌텐도 스위치 프로콘", "닌텐도 스위치 도크"],
    "플레이스테이션5": ["플레이스테이션5 디지털에디션", "플레이스테이션5 슬림", "PS5 번들", "PS5 프로"],
}

BASE_MAX_SCROLLS = 15
EXPANDED_MAX_SCROLLS = 30
TARGET_TOTAL = 4000
SAVE_DIR = "번개장터"
os.makedirs(SAVE_DIR, exist_ok=True)

# media.bunjang.co.kr 도메인의 이미지 URL 패턴 (API 응답 본문 + 렌더된 HTML 양쪽에서 탐지)
IMAGE_URL_RE = re.compile(r'https://media\.bunjang\.co\.kr/product/[^\s"\'\\]+?\.(?:jpg|jpeg|png|webp)')

def extract_image_urls(text):
    return set(IMAGE_URL_RE.findall(text))

def current_total():
    return len([f for f in os.listdir(SAVE_DIR) if os.path.isfile(os.path.join(SAVE_DIR, f))])

async def crawl_keyword(keyword, seen_urls, max_scrolls):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
        )

        collected = set()

        async def on_response(response):
            if "bunjang.co.kr" in response.url:
                try:
                    body = await response.text()
                    collected.update(extract_image_urls(body))
                except Exception:
                    pass

        page.on("response", on_response)

        search_url = f"https://m.bunjang.co.kr/search/products?q={keyword}"
        await page.goto(search_url, wait_until="networkidle")

        for _ in range(max_scrolls):
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(1500)

        # 폴백: 렌더된 HTML에서도 한 번 더 추출
        html = await page.content()
        collected.update(extract_image_urls(html))

        await browser.close()

        new_urls = collected - seen_urls
        seen_urls.update(new_urls)
        print(f"[{keyword}] 새로 수집된 이미지: {len(new_urls)}개")
        return new_urls

def download_images(urls):
    for url in urls:
        try:
            ext = url.split(".")[-1].split("?")[0]
            # hash()는 프로세스마다 시드가 달라져 재실행 시 같은 이미지가 다른 파일명으로 저장됨 -> md5로 안정적인 파일명 생성
            digest = hashlib.md5(url.encode()).hexdigest()
            filename = os.path.join(SAVE_DIR, f"img_{digest}.{ext}")
            if os.path.exists(filename):
                continue
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                with open(filename, "wb") as f:
                    f.write(resp.content)
            time.sleep(0.3)  # 서버 부담 줄이기용 딜레이
        except Exception as e:
            print(f"다운로드 실패: {url} ({e})")

async def crawl_and_download(keyword, seen, max_scrolls):
    urls = await crawl_keyword(keyword, seen, max_scrolls)
    download_images(urls)
    return current_total()

async def main():
    seen = set()
    total = current_total()
    print(f"시작 시점 이미지 개수: {total}")

    # 1차: 기본 키워드, 기본 스크롤 횟수
    for kw in KEYWORDS:
        if total >= TARGET_TOTAL:
            break
        total = await crawl_and_download(kw, seen, BASE_MAX_SCROLLS)
        print(f"누적 이미지 개수: {total}")

    # 2차: 목표 미달 시 스크롤 횟수를 늘려 기본 키워드 재수집
    if total < TARGET_TOTAL:
        print(f"목표({TARGET_TOTAL}) 미달, MAX_SCROLLS를 {EXPANDED_MAX_SCROLLS}로 늘려 재수집합니다.")
        for kw in KEYWORDS:
            if total >= TARGET_TOTAL:
                break
            total = await crawl_and_download(kw, seen, EXPANDED_MAX_SCROLLS)
            print(f"누적 이미지 개수: {total}")

    # 3차: 여전히 미달 시 세부 검색어로 쪼개서 추가 수집
    if total < TARGET_TOTAL:
        print(f"목표({TARGET_TOTAL}) 미달, 세부 검색어로 추가 수집합니다.")
        for kw in KEYWORDS:
            if total >= TARGET_TOTAL:
                break
            for sub_kw in SUB_KEYWORDS.get(kw, []):
                if total >= TARGET_TOTAL:
                    break
                total = await crawl_and_download(sub_kw, seen, EXPANDED_MAX_SCROLLS)
                print(f"누적 이미지 개수: {total}")

    print(f"최종 {SAVE_DIR} 폴더 전체 이미지 개수: {current_total()}")

if __name__ == "__main__":
    asyncio.run(main())
