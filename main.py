"""매일 아침 AI 주식 브리핑 -> 텔레그램

흐름: 시세(yfinance) + 뉴스(Google News RSS) 수집 -> LLM 분석 -> 텔레그램 전송
LLM 선택: 환경변수 LLM_PROVIDER = gemini(기본, 무료 티어) | claude
로컬 테스트: DRY_RUN=1 python main.py  (텔레그램 전송 없이 결과만 출력)
"""
import datetime as dt
import os
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

KST = ZoneInfo("Asia/Seoul")


def env(name, default=""):
    # GitHub Actions는 값이 없는 변수도 빈 문자열로 넘기므로 `or`로 기본값 처리
    return os.getenv(name) or default


TELEGRAM_TOKEN = env("TELEGRAM_TOKEN")
CHAT_ID = env("CHAT_ID")
LLM_PROVIDER = env("LLM_PROVIDER", "gemini").lower()
# 모델명은 자주 바뀌므로 환경변수로 교체 가능, 쉼표로 여러 개 주면 앞에서부터 순서대로 시도
GEMINI_MODELS = [m.strip() for m in env("GEMINI_MODEL", "gemini-3.5-flash,gemini-2.5-flash").split(",") if m.strip()]
CLAUDE_MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")

# ---------- 수집 대상 (여기만 고치면 종목/지표 추가 가능) ----------
US_MARKET = {
    "필라델피아 반도체지수(SOX)": "^SOX",
    "나스닥 종합": "^IXIC",
    "S&P 500": "^GSPC",
    "다우존스": "^DJI",
    "엔비디아": "NVDA",
    "마이크론": "MU",
    "TSMC(ADR)": "TSM",
    "미 10년물 금리(%)": "^TNX",
    "VIX(공포지수)": "^VIX",
    "원/달러 환율": "KRW=X",
    "WTI 유가": "CL=F",
}
KR_MARKET = {"코스피": "^KS11", "코스닥": "^KQ11"}
WATCHLIST = {"삼성전자": "005930.KS", "SK하이닉스": "000660.KS"}

# (표시 이름, 검색어, 언어)  -- 미국 이슈는 영어 기사가 훨씬 빠르고 풍부함
NEWS_QUERIES = [
    ("한국 경제·정책", "한국 경제 OR 기준금리 OR 수출 OR 환율 OR 코스피", "ko"),
    ("미국 경제지표·연준", "Fed OR CPI OR jobs report OR Treasury yields OR tariffs", "en"),
    ("지정학·전쟁", "war OR ceasefire OR sanctions OR Middle East OR Ukraine OR Taiwan", "en"),
    ("미국 증시·반도체", "Wall Street OR Nasdaq OR S&P 500 OR Nvidia OR Micron OR TSMC OR HBM", "en"),
]

DISCLAIMER = "※ 공개 뉴스·시세를 AI가 요약·추론한 참고 자료입니다. 투자 판단과 책임은 본인에게 있습니다."


# ---------- 시세 ----------
def get_quote(symbol, retries=3):
    """최근 거래일 종가/전일 대비/거래량. 실패하면 None (클라우드 IP에서 yfinance가 가끔 막히므로 재시도)"""
    for i in range(retries):
        try:
            df = yf.Ticker(symbol).history(period="10d").dropna(subset=["Close"])
            if len(df) >= 2:
                last, prev = df.iloc[-1], df.iloc[-2]
                vol_avg = df["Volume"].iloc[:-1].tail(5).mean()
                return {
                    "date": df.index[-1].strftime("%Y-%m-%d"),
                    "close": float(last["Close"]),
                    "diff": float(last["Close"] - prev["Close"]),
                    "pct": float((last["Close"] / prev["Close"] - 1) * 100),
                    "volume": float(last["Volume"]),
                    "vol_ratio": float(last["Volume"] / vol_avg) if vol_avg and vol_avg == vol_avg else None,
                }
        except Exception as e:
            print(f"[경고] {symbol} 시세 수집 실패 ({i + 1}/{retries}): {e}")
        time.sleep(2 * (i + 1))
    return None


def fmt_quote(name, q, with_volume=False):
    if not q:
        return f"- {name}: 데이터 수집 실패"
    line = f"- {name}: {q['close']:,.2f} (전일比 {q['diff']:+,.2f}, {q['pct']:+.2f}%, 기준일 {q['date']})"
    if with_volume and q["vol_ratio"]:
        line += f", 거래량 {q['volume']:,.0f}주 (직전 5거래일 평균의 {q['vol_ratio']:.1f}배)"
    return line


# ---------- 뉴스 ----------
def google_news(query, lang="ko", n=6, when="1d"):
    """구글 뉴스 RSS에서 최근 기사 제목/시각/링크. 검색어는 requests가 URL 인코딩해 줌"""
    hl, gl, ceid = {"ko": ("ko", "KR", "KR:ko"), "en": ("en-US", "US", "US:en")}[lang]
    try:
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": f"{query} when:{when}", "hl": hl, "gl": gl, "ceid": ceid},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"[경고] 뉴스 수집 실패 ({query[:30]}...): {e}")
        return []

    seen, items = set(), []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(KST).strftime("%m-%d %H:%M")
        except Exception:
            pub = "시각 불명"
        items.append(f"- [{pub} KST] {title}\n  {(it.findtext('link') or '').strip()}")
        if len(items) >= n:
            break
    return items


def build_data_text(now):
    when = "3d" if now.weekday() == 0 else "1d"  # 월요일엔 주말 뉴스까지 포함
    blocks = []

    blocks.append("## 미국 시장 (직전 미국 거래일 기준)\n" + "\n".join(fmt_quote(n, get_quote(s)) for n, s in US_MARKET.items()))
    blocks.append("## 한국 시장 (직전 한국 거래일 기준)\n" + "\n".join(fmt_quote(n, get_quote(s)) for n, s in KR_MARKET.items()))
    blocks.append("## 관심 종목\n" + "\n".join(fmt_quote(n, get_quote(s), with_volume=True) for n, s in WATCHLIST.items()))

    for label, query, lang in NEWS_QUERIES:
        news = google_news(query, lang, n=6, when=when)
        blocks.append(f"## 뉴스: {label}\n" + ("\n".join(news) if news else "수집된 기사 없음"))

    for name in WATCHLIST:
        news = google_news(f"{name} 주가 OR {name} 실적 OR {name} 전망", "ko", n=6, when=when)
        blocks.append(f"## 뉴스: {name}\n" + ("\n".join(news) if news else "수집된 기사 없음"))

    return "\n\n".join(blocks)


# ---------- 프롬프트 ----------
SYSTEM_PROMPT = """당신은 한국 개인 투자자를 위한 아침 브리핑을 쓰는 시니어 주식 애널리스트입니다.

원칙:
1. [데이터]에 있는 내용만 근거로 삼습니다. 없는 수치·사건·기사·URL을 지어내지 마세요. 데이터에 없는 항목(외국인/기관 수급, 야간선물, 실적 발표 일정 등)은 '확인 불가'라고 하거나 언급하지 않습니다.
2. 수치는 데이터 그대로 인용합니다. 기준일이 직전 거래일이 아니거나 휴장·주말이면 그 사실을 한 줄로 밝힙니다.
3. 원인 분석은 뉴스로 뒷받침되면 단정하고, 뉴스 근거가 없으면 '~때문으로 추정'처럼 추정임을 밝힙니다. 서로 엇갈리는 신호는 숨기지 말고 함께 적습니다.
4. 투자의견은 매수/보류/매도 중 하나로 명확히 주되, 핵심 근거, 확신도(높음/중간/낮음), '어떤 상황이 오면 의견이 바뀌는지'를 반드시 함께 적습니다. 근거가 약하면 보류를 택하고 이유를 설명하세요.
5. 출력은 텔레그램용 순수 텍스트입니다. 마크다운 기호(**, #, 백틱, 표)를 쓰지 말고, 섹션 제목은 이모지로, 목록은 '-'로 씁니다. 전체 2,800자 이내."""


def build_user_prompt(now, data):
    day = "월화수목금토일"[now.weekday()]
    names = " / ".join(WATCHLIST)
    return f"""오늘은 {now:%Y-%m-%d}({day}) 아침입니다. 아래 데이터로 브리핑을 작성하세요.

[데이터]
{data}

[출력 형식 - 이 3개 섹션만, 이 순서로]

1️⃣ 한·미 주요 이슈 (경제지표·정책·전쟁 등)
- 주식시장에 영향을 줄 이슈를 골라 분석·요약. 최대 5줄. 단순 나열 말고 '그래서 시장에 어떤 의미인지'까지.
- 정말 중요한 기사가 있을 때만 데이터에 있는 URL을 그대로 1~2개 첨부. 없으면 링크 생략.

2️⃣ 간밤 미국 증시
- SOX·나스닥·S&P500(필요시 다우)의 등락률, 그리고 왜 그렇게 움직였는지. 엔비디아·마이크론·금리·VIX·환율·유가 등 데이터로 설명되는 부분은 연결해서 설명. 5줄 내외.

3️⃣ 관심 종목: {names} (종목별로)
- 어제 움직임과 원인 (거래량 포함)
- 오늘 전망: 1·2번 내용과 연결한 시나리오 (강세로 갈 조건 / 약세로 갈 조건)
- 투자의견: 매수/보류/매도 + 확신도 + 핵심 근거 + 의견이 바뀌는 조건"""


# ---------- LLM ----------
def call_gemini(system, prompt):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    last_err = None
    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        temperature=0.3,
                        max_output_tokens=8192,  # 추론 토큰이 포함될 수 있어 넉넉히
                    ),
                )
                if resp.text:
                    print(f"[정보] Gemini 모델 사용: {model}")
                    return resp.text
                last_err = "빈 응답"
            except Exception as e:
                last_err = e
                if "404" in str(e) or "NOT_FOUND" in str(e):
                    break  # 모델이 없어졌으면 재시도 말고 다음 모델로
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Gemini 호출 실패: {last_err}")


def call_claude(system, prompt):
    import anthropic

    client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"), max_retries=3)
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if b.type == "text")


def call_llm(system, prompt):
    if LLM_PROVIDER == "claude":
        return call_claude(system, prompt)
    if LLM_PROVIDER == "gemini":
        return call_gemini(system, prompt)
    raise ValueError(f"알 수 없는 LLM_PROVIDER: {LLM_PROVIDER}")


# ---------- 텔레그램 ----------
def split_message(text, limit=4000):
    """텔레그램은 메시지당 4096자 제한 -> 문단 단위로 나눔"""
    parts, buf = [], ""
    for para in text.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > limit:
            parts.append(buf.strip())
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        parts.append(buf.strip())

    final = []
    for p in parts:
        while len(p) > limit:  # 한 문단이 한도를 넘는 극단적인 경우
            final.append(p[:limit])
            p = p[limit:]
        final.append(p)
    return final


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in split_message(text):
        # parse_mode를 쓰지 않음: LLM이 만든 텍스트의 특수문자 때문에 전송이 거부되는 일을 방지
        r = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": chunk, "link_preview_options": {"is_disabled": True}},
            timeout=20,
        )
        if not r.ok:
            raise RuntimeError(f"텔레그램 전송 실패: {r.status_code} {r.text}")
        time.sleep(1)


# ---------- 실행 ----------
def main():
    dry_run = bool(env("DRY_RUN"))
    missing = [k for k in ("TELEGRAM_TOKEN", "CHAT_ID") if not env(k) and not dry_run]
    if missing:
        sys.exit(f"환경변수 누락: {missing}")

    now = dt.datetime.now(KST)
    try:
        print("데이터 수집 중...")
        data = build_data_text(now)
        print(data)  # 모델이 실제로 본 입력 확인용 (Actions 로그에서 점검 가능)

        print(f"AI 분석 중... ({LLM_PROVIDER})")
        report = call_llm(SYSTEM_PROMPT, build_user_prompt(now, data))

        message = f"📊 {now:%m/%d} 아침 브리핑\n\n{report.strip()}\n\n{DISCLAIMER}"
        if dry_run:
            print("\n" + "=" * 40 + "\n" + message)
            return
        send_telegram(message)
        print("✅ 전송 완료")
    except Exception as e:
        print(f"[오류] {e}")
        if not dry_run:
            try:
                send_telegram(f"⚠️ 브리핑 생성 실패: {e}")  # 조용히 실패하지 않도록 알림
            except Exception:
                pass
        sys.exit(1)  # Actions에서 실패(빨간불)로 표시


if __name__ == "__main__":
    main()
