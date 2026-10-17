import os
import requests
import datetime
import yfinance as yf

# 1. 텔레그램 인증 정보 (환경변수가 없으면 전달해주신 값 기본 사용)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8712648987:AAFzxlUAV2xl4D9RgZlwk-NE6TXasilebCw")
CHAT_ID = os.getenv("CHAT_ID", "8664952268")

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload)
        if response.status_code == 200:
            print("✅ 텔레그램 메시지 전송 성공!")
        else:
            print(f"❌ 전송 실패: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ 오류 발생: {e}")

def get_market_summary():
    # 모니터링 대상 지수 및 주요 종목
    items = {
        "🇺🇸 S&P 500": "^GSPC",
        "🇺🇸 나스닥 (NASDAQ)": "^IXIC",
        "🇺🇸 엔비디아 (NVDA)": "NVDA",
        "🇺🇸 애플 (AAPL)": "AAPL",
        "🇰🇷 코스피 (KOSPI)": "^KS11",
        "🇰🇷 코스닥 (KOSDAQ)": "^KQ11",
        "🇰🇷 삼성전자": "005930.KS"
    }

    now_str = datetime.datetime.now().strftime("%Y-%m-%d")
    report = [f"📊 **[{now_str}] 일간 주식 시장 모니터링 리포트**\n"]
    report.append("🌐 **[증시 동향 및 주요 종목 요약]**")

    for name, symbol in items.items():
        try:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period="5d")
            if len(df) >= 2:
                prev_close = df['Close'].iloc[-2]
                curr_close = df['Close'].iloc[-1]
                change_pct = ((curr_close - prev_close) / prev_close) * 100
                
                emoji = "🔺" if change_pct > 0 else ("🔻" if change_pct < 0 else "➖")
                report.append(f"• **{name}**: {curr_close:,.2f} ({emoji} {change_pct:+.2f}%)")
        except Exception as e:
            report.append(f"• **{name}**: 데이터 조회 실패")

    report.append("\n💡 **[오늘의 체크포인트]**")
    report.append("• 미국 주요 증시 종가 및 기술주 흐름 점검")
    report.append("• 환율 변동성 및 개장 전 주요 이슈 확인 권장")

    return "\n".join(report)

if __name__ == "__main__":
    print("주식 데이터 수집 및 리포트 생성 중...")
    report_text = get_market_summary()
    send_telegram_message(report_text)