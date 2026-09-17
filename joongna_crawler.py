# pip install playwright requests
# playwright install chromium
#
# [1단계 탐지 결과]
# web.joongna.com 검색 페이지에서 실제 상품 이미지는 img2.joongna.com/media/original/... 도메인/경로로 서빙됨.
# (쿠팡/네이버 광고 썸네일(coupangcdn.com, pstatic.net)과 광고 CDN(adsappier.com)은 검색결과에 섞여 나오지만
#  상품 이미지가 아니므로 정규식에서 제외)
# 쿼리스트링을 제거하면(예: ?impolicy=thumb&size=150 제거) img2.joongna.com/media/original/... 원본 크기 이미지를
# 그대로 받아올 수 있어, 다운로드 시에는 쿼리를 제거한 URL을 사용.

import asyncio, re, time, os, sys, hashlib, html, json
from urllib.parse import quote
from collections import defaultdict
from playwright.async_api import async_playwright
import requests

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 일반 카테고리 (정상적인 일반 중고 사진 다양성 확보용)
GENERAL_KEYWORDS = [
    "유아용품", "도서", "주방용품", "소파", "책상", "자전거", "캠핑용품", "화장품", "향수",
    "스피커", "헤드폰", "게임기", "프린터", "반려동물용품", "운동기구", "골프채", "악기",
    "시계", "가방", "신발",
]

# 마모/손상이 흔해서 AI 보정("흠집 지우기", "새 제품처럼 보이게 하기")으로 조작할 유인이 큰 카테고리
DAMAGE_PRONE_KEYWORDS = [
    "명품가방", "명품지갑", "카메라", "노트북", "그래픽카드",
    "세탁기", "냉장고", "원목가구", "자동차부품", "스니커즈", "명품시계",
]

KEYWORDS = GENERAL_KEYWORDS + DAMAGE_PRONE_KEYWORDS  # 총 31개 (요구사항 25개 이상 충족)

# 목표 개수 미달 시 세부 검색어로 쪼개서 추가 수집 (마모/손상·조작 유인 큰 카테고리 위주)
SUB_KEYWORDS = {
    "명품가방": ["샤넬 클래식백", "루이비통 스피디", "구찌 마몬트", "프라다 리에디션"],
    "명품지갑": ["샤넬 반지갑", "루이비통 지퍼지갑", "구찌 장지갑", "프라다 카드지갑"],
    "카메라": ["캐논 DSLR", "소니 미러리스", "니콘 카메라", "후지필름 카메라"],
    "노트북": ["맥북 프로", "맥북 에어", "LG 그램", "삼성 갤럭시북"],
    "그래픽카드": ["RTX4090", "RTX4080", "RTX3080", "RTX3070"],
    "세탁기": ["드럼세탁기", "통돌이세탁기", "미니세탁기"],
    "냉장고": ["김치냉장고", "양문형냉장고", "미니냉장고"],
    "원목가구": ["원목 식탁", "원목 책상", "원목 의자", "원목 침대"],
    "자동차부품": ["자동차 휠", "자동차 범퍼", "블랙박스", "네비게이션"],
    "스니커즈": ["나이키 덩크", "조던1", "뉴발란스 992", "아식스 젤카야노"],
    "명품시계": ["롤렉스", "오메가 시계", "까르띠에 시계", "태그호이어"],
}

BASE_MAX_SCROLLS = 20
EXPANDED_MAX_SCROLLS = 35
TARGET_TOTAL = 10000
SAVE_DIR = "joongna_images"
os.makedirs(SAVE_DIR, exist_ok=True)

# 중간에 (메모리 부족 등으로) 프로세스가 죽어도 이어서 실행할 수 있도록
# "어느 pass에서 어느 키워드까지 끝났는지" + 키워드별 누적 개수를 파일에 기록해둔다.
STATE_FILE = "joongna_state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return set(tuple(x) for x in data.get("completed", [])), data.get("keyword_counts", {})
        except Exception:
            pass
    return set(), {}


def save_state(completed, counts):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"completed": list(completed), "keyword_counts": counts}, f, ensure_ascii=False, indent=2)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# img2.joongna.com의 실제 상품 이미지 URL 패턴 (API 응답 본문 + 렌더된 HTML 양쪽에서 탐지)
IMAGE_URL_RE = re.compile(
    r'https://img2\.joongna\.com/media/original/[^\s"\'\\<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\s"\'\\<>]*)?',
    re.IGNORECASE,
)

# 키워드별 신규 수집 이미지 개수 누적 (4단계 요약용) - 재실행 시 이전 state를 이어받음
_completed_pairs, _saved_counts = load_state()
keyword_counts = defaultdict(int, _saved_counts)


def extract_image_urls(text):
    # 렌더된 HTML/JSON 응답 속 &amp; 이스케이프를 정규화한 뒤 추출
    unescaped = html.unescape(text)
    found = IMAGE_URL_RE.findall(unescaped)
    # 쿼리스트링 차이(?impolicy=thumb&size=150 vs 없음)로 인한 중복을 막기 위해 base URL로 정규화
    return {u.split("?")[0] for u in found}


def current_total():
    return len([f for f in os.listdir(SAVE_DIR) if os.path.isfile(os.path.join(SAVE_DIR, f))])


async def crawl_keyword(keyword, seen_urls, max_scrolls):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(user_agent=UA)

        # 이미지 URL은 검색 API JSON 응답 본문에서 이미 추출되므로, 브라우저가 실제 이미지
        # 바이너리를 받아 디코딩/렌더링할 필요가 없다. 메모리 사용량을 크게 줄이기 위해
        # 이미지/미디어/폰트 리소스 요청 자체를 네트워크 레벨에서 차단한다.
        # 광고/트래킹 도메인(쿠팡, 네이버 광고, adsappier, 구글 광고 등)도 상품 이미지가 아니라
        # 어차피 정규식에서 제외되는 대상이므로 아예 차단해 메모리/네트워크 부하를 더 줄인다.
        AD_TRACKING_DOMAINS = (
            "adsappier.com", "appiersig.com", "appier.net",
            "googlesyndication.com", "doubleclick.net", "google-analytics.com",
            "googletagmanager.com", "adtrafficquality.google",
            "coupang.com", "coupangcdn.com", "pstatic.net",
            "sentry.joongna.com",
        )

        async def block_heavy_resources(route):
            req = route.request
            url = req.url
            if any(d in url for d in AD_TRACKING_DOMAINS):
                await route.abort()
                return
            if req.resource_type in ("image", "media", "font"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_heavy_resources)

        collected = set()

        async def on_response(response):
            url = response.url
            if "joongna.com" not in url:
                return
            # 응답 자체가 상품 이미지 URL인 경우
            if IMAGE_URL_RE.match(url):
                collected.add(url.split("?")[0])
            # 응답 바디(검색 API의 JSON 등) 안에 이미지 URL 문자열이 있는 경우
            try:
                ctype = response.headers.get("content-type", "")
                if "image" in ctype:
                    return
                body = await response.text()
                collected.update(extract_image_urls(body))
            except Exception:
                pass

        page.on("response", on_response)

        search_url = f"https://web.joongna.com/search/{quote(keyword, safe='')}"
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=30000)
        except Exception:
            pass  # networkidle 타임아웃이어도 이미 로드된 내용으로 계속 진행

        for _ in range(max_scrolls):
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(1200)

        # 폴백: 렌더된 HTML의 <img> 태그 src / data-src 도 반영
        rendered_html = await page.content()
        collected.update(extract_image_urls(rendered_html))

        await browser.close()

        new_urls = collected - seen_urls
        seen_urls.update(new_urls)
        print(f"[{keyword}] 새로 수집된 이미지: {len(new_urls)}개")
        return new_urls


def download_images(urls):
    for url in urls:
        try:
            ext = url.split(".")[-1].split("?")[0].lower()
            if ext not in ("jpg", "jpeg", "png", "webp"):
                ext = "jpg"
            # hash()는 프로세스마다 시드가 달라져 재실행 시 파일명이 바뀜 -> md5로 안정적인 파일명 생성
            digest = hashlib.md5(url.encode()).hexdigest()
            filename = os.path.join(SAVE_DIR, f"img_{digest}.{ext}")
            if os.path.exists(filename):
                continue
            resp = requests.get(url, timeout=10, headers={"User-Agent": UA})
            if resp.status_code == 200:
                with open(filename, "wb") as f:
                    f.write(resp.content)
            time.sleep(0.3)  # 서버 부담 줄이기용 딜레이
        except Exception as e:
            print(f"다운로드 실패: {url} ({e})")


async def crawl_and_download(keyword, seen, max_scrolls, pass_name):
    urls = await crawl_keyword(keyword, seen, max_scrolls)
    keyword_counts[keyword] += len(urls)
    download_images(urls)
    total = current_total()
    _completed_pairs.add((pass_name, keyword))
    save_state(_completed_pairs, dict(keyword_counts))
    return total


async def main():
    seen = set()
    total = current_total()
    print(f"시작 시점 이미지 개수: {total}")

    # 1차: 기본 키워드, 기본 스크롤 횟수
    for kw in KEYWORDS:
        if total >= TARGET_TOTAL:
            break
        if ("pass1", kw) in _completed_pairs:
            print(f"[{kw}] pass1 이미 완료됨, 건너뜀")
            continue
        total = await crawl_and_download(kw, seen, BASE_MAX_SCROLLS, "pass1")
        print(f"누적 이미지 개수: {total}")

    # 2차: 목표 미달 시 스크롤 횟수를 늘려 기본 키워드 재수집
    if total < TARGET_TOTAL:
        print(f"목표({TARGET_TOTAL}) 미달, MAX_SCROLLS를 {EXPANDED_MAX_SCROLLS}로 늘려 재수집합니다.")
        for kw in KEYWORDS:
            if total >= TARGET_TOTAL:
                break
            if ("pass2", kw) in _completed_pairs:
                print(f"[{kw}] pass2 이미 완료됨, 건너뜀")
                continue
            total = await crawl_and_download(kw, seen, EXPANDED_MAX_SCROLLS, "pass2")
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
                if ("pass3", sub_kw) in _completed_pairs:
                    print(f"[{sub_kw}] pass3 이미 완료됨, 건너뜀")
                    continue
                total = await crawl_and_download(sub_kw, seen, EXPANDED_MAX_SCROLLS, "pass3")
                print(f"누적 이미지 개수: {total}")

    # 4단계: 완료 후 요약 출력
    final_total = current_total()
    print(f"\n최종 {SAVE_DIR} 폴더 전체 이미지 개수: {final_total}")
    print("\n=== 키워드(카테고리)별 신규 수집 이미지 개수 ===")
    for kw, cnt in sorted(keyword_counts.items(), key=lambda x: -x[1]):
        print(f"{cnt:5d}  {kw}")


if __name__ == "__main__":
    asyncio.run(main())
