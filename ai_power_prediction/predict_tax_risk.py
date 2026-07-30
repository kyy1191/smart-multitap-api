"""
12V 소형 환경(스마트 멀티탭) 커스텀 누진세 구간 진입 확률 + 월말 예상 요금 예측.

실제 Supabase `sensor_data` 테이블(main.py가 쓰는 것과 동일 테이블, 동일 스키마)을 그대로 읽는다.
새 테이블은 만들지 않음 (2026-07-30 사용자 확인).

단위/스케일 (2026-07-30 사용자 확인, 3번째 확인에서 방향 수정됨):
- 실측 하드웨어는 current/power를 mA/mW로 보고한다 (A/W 아님). 실제 연결된 부하가
  LED 테스트 회로 수준(mW급)이라, 원시값 그대로 쓰면 한 달 내내 켜놔도 200Wh/400Wh
  구간에 전혀 안 걸려서 위험도가 항상 0%로만 나옴 — 데모 관점에서 의미가 없음.
- 그렇다고 배율을 고정값(예: ×1000)으로 세게 걸면 반대로 첫날부터 항상 100%
  최고위험에 고정돼서 게이지/그래프에 변화가 안 보임 (실제로 ×1000 테스트해보고 발견).
- 그래서 기본은 "auto" 모드: 지금까지 관측된 하루 평균 사용 패턴을 그대로 한 달간
  유지한다고 가정했을 때, 월말 예상치가 TARGET_MONTH_TOTAL_WH(기본 2구간 임계값의
  1.15배, 즉 2구간 경계 바로 위)에 오도록 배율을 역산한다. 그래야 그래프가 월초엔
  낮다가 시간이 지날수록 2/3구간 쪽으로 점진적으로 다가가는 자연스러운 모양이 됨.
  LOAD_EXTRAPOLATION_FACTOR 환경변수에 숫자를 직접 넣으면 auto 대신 그 고정값을 씀.

이 파일은 main.py와 완전히 분리된 독립 모듈이다 (main.py는 다른 세션이 지금 작업 중이라 락 걸려있음,
2026-07-30). main.py에 합칠 때는 이 파일을 그대로 import해서 쓰면 된다.

예측 엔진 (2026-07-30 추가): 월말 예상치/구간확률/신뢰구간은 기본적으로 **Gemini(클라우드 AI)**가
직접 추정한다 (main.py의 화재판단 AI와 같은 SDK/모델, 별도 호출). 실측 일별 사용량(환산 후 Wh)을
요약해서 프롬프트로 주고 구조화된 JSON으로 예측치를 받는다. GEMINI_API_KEY가 없거나 호출이
실패하면 자동으로 기존 몬테카를로 부트스트랩 시뮬레이션으로 폴백한다 (완전히 죽지 않게).
"""
from __future__ import annotations

import json
import os
import random
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from calendar import monthrange
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase import create_client, Client

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    genai = None
    genai_types = None

try:
    from groq import Groq
except ImportError:
    Groq = None


# ── 설정 (모두 환경변수로 오버라이드 가능) ────────────────────────────────
TABLE_NAME = os.environ.get("POWER_PREDICTION_TABLE", "sensor_data")
TZ_NAME = os.environ.get("POWER_PREDICTION_TZ", "Asia/Seoul")
DEVICE_ID = os.environ.get("POWER_PREDICTION_DEVICE_ID", "smart_multitap_1")

# 실제 하드웨어가 연결된 포트만 집계 (2026-07-30 기준 포트 1,2만 실물 연결,
# 포트 3,4는 무부하/테스트값 — reference_smart_multitap_infra 메모 참고).
# 나중에 포트가 늘어나면 이 값만 바꾸면 됨.
ACTIVE_PORTS = [
    int(p) for p in os.environ.get("POWER_PREDICTION_ACTIVE_PORTS", "1,2").split(",") if p.strip()
]

# 2026-07-30 밤 재설계: "사용량을 부풀려서 220V 가전처럼 보이게" 하는 대신,
# 실측값은 있는 그대로(raw, 부풀리기 없음) 쓰고 — 대신 "구간 임계값 자체"와
# "요금 단가"를 12V/220V 전압비로 환산한다. 근거: 같은 전류를 흘렸을 때
# Wh = V × I × t 이므로, 같은 전류 기준으로는 12V에서의 Wh가 220V에서의 Wh보다
# 정확히 (12/220)배 작다. 그래서:
#   - 구간 임계값(200Wh/400Wh)도 (12/220)배로 낮춰서, 실측 raw 값이 실제로
#     그 구간에 자연스럽게 도달할 수 있게 함 (사용자 확정, 2026-07-30)
#   - 요금 단가(원/kWh)는 반대로 (220/12)배 키워서, "같은 실제 전류 사용량"에
#     대해 220V든 12V든 최종 요금(원)이 동일하게 나오도록 맞춤
VOLTAGE_RATIO = float(os.environ.get("POWER_PREDICTION_VOLTAGE_RATIO", 12.0 / 220.0))

# 실측값 자체는 더 이상 배율로 부풀리지 않음 (기본 1.0 = raw 그대로).
# 예전 "auto" 모드(추세를 목표치에 맞춰 역산)는 더 이상 기본이 아니지만,
# 필요하면 LOAD_EXTRAPOLATION_FACTOR=auto로 언제든 되돌릴 수 있게 남겨둠.
LOAD_EXTRAPOLATION_FACTOR_ENV = os.environ.get("LOAD_EXTRAPOLATION_FACTOR", "1").strip().lower()
TARGET_MONTH_TOTAL_WH = float(os.environ.get("TAX_RISK_TARGET_MONTH_WH", 0))  # auto 모드에서만 씀, 0이면 BRACKET_2_WH*1.15
LOAD_EXTRAPOLATION_FALLBACK_FACTOR = float(os.environ.get("LOAD_EXTRAPOLATION_FALLBACK_FACTOR", 1000.0))

# 커스텀 누진세 구간: 실제 220V 기준(200kWh/400kWh → 1/1000 축소해 200Wh/400Wh)에서
# 다시 전압비(12/220)만큼 낮춤 — 기본값 약 10.9Wh / 21.8Wh.
BRACKET_2_WH = float(os.environ.get("TAX_BRACKET_2_WH", 200)) * VOLTAGE_RATIO
BRACKET_3_WH = float(os.environ.get("TAX_BRACKET_3_WH", 400)) * VOLTAGE_RATIO
TIER1_MAX_WH = BRACKET_2_WH
TIER2_MAX_WH = BRACKET_3_WH
if TARGET_MONTH_TOTAL_WH <= 0:
    TARGET_MONTH_TOTAL_WH = BRACKET_2_WH * 1.15

# 요금: 2026-07-30 밤 재조정 — 게이지/구간확률은 작게 축소된 실측 기준(BRACKET_2/3_WH)을 쓰지만,
# "요금(원)"은 그렇게 계산하면 몇천 원대로 나와서 사람이 체감하는 실제 전기요금 느낌이 안 남.
# 그래서 요금 계산 시에만 raw 사용량을 "220V-등가"로 되돌려서(×220/12), 실제 한전 200kWh/400kWh
# 구간·실제 원/kWh 단가에 그대로 대입 — 그래야 2구간 근처에서 실제 가정 전기요금과 비슷한
# 2~3만원대가 나와서 시연할 때 사람들이 체감할 수 있음 (2026-07-30 사용자 확정).
# 실제 한전 주택용 저압 요금(기본요금 910/1,600/7,300원, 전력량요금 120.0/214.6/307.3원/kWh — 웹 검색 확인).
TIER_BASE_FEE_WON = {1: 910, 2: 1_600, 3: 7_300}
TIER_RATE_WON_PER_WH = {1: 120.0, 2: 214.6, 3: 307.3}  # 실제 원/kWh 값을 그대로 재사용 (요금 계산용 220V-등가 입력에 대입)
FEE_TIER1_MAX_WH = 200.0  # 요금 계산 전용 - 실제 한전 구간(200kWh를 그대로 Wh로 relabel), 축소된 BRACKET_*_WH와는 다름
FEE_TIER2_MAX_WH = 400.0

MC_SIMULATIONS = int(os.environ.get("POWER_PREDICTION_MC_SIMS", 5000))
FALLBACK_NOISE_STD = float(os.environ.get("POWER_PREDICTION_FALLBACK_NOISE_STD", 0.35))

# 예측 엔진: "groq"(기본, 2026-07-30 변경) = Groq(무료 토큰 넉넉, Llama 계열 초고속 추론)가
# 함수 호출(tool calling)로 실제 몬테카를로 시뮬레이션을 직접 실행시킨 뒤, 그 결과를 바탕으로
# 최종 예측치/판단근거를 생성. "gemini" = 예전 방식(Gemini가 직접 숫자를 추정, 도구 없이).
# "montecarlo"면 AI 호출 자체를 안 하고 통계 시뮬레이션만 사용.
PREDICTION_ENGINE = os.environ.get("PREDICTION_ENGINE", "groq").strip().lower()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("PREDICTION_GEMINI_MODEL", "gemini-flash-latest")  # main.py와 동일 모델
# Gemini 무료 티어 쿼터가 모델당 하루 20회밖에 안 됨(실측으로 429 RESOURCE_EXHAUSTED 확인,
# 2026-07-30) - main.py의 화재/낭비판단 AI도 같은 모델이라 쿼터를 나눠 씀. 이게 바로
# PREDICTION_ENGINE 기본값을 Groq로 바꾼 이유(무료 쿼터가 훨씬 넉넉함).
GEMINI_CACHE_TTL_SECONDS = int(os.environ.get("PREDICTION_GEMINI_CACHE_TTL_SECONDS", 7200))

# .env에 "groq_API_key"로 들어있는 걸 우선 인식 (대문자 GROQ_API_KEY도 허용)
GROQ_API_KEY = os.environ.get("groq_API_key") or os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("PREDICTION_GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_CACHE_TTL_SECONDS = int(os.environ.get("PREDICTION_GROQ_CACHE_TTL_SECONDS", 300))

_ai_cache: dict = {"result": None, "computed_at": 0.0}

# postgrest 페이지네이션 안전장치 — 한 달치 고빈도 원시 로그를 전부 끌어오면
# 너무 커질 수 있어 최근 N행으로 캡. 일 평균만 필요하므로 통계적으로는 충분하지만,
# 트래픽이 훨씬 커지면 DB 쪽에 날짜별 집계 뷰/RPC를 만드는 게 더 정확함 (지금은 오버엔지니어링이라 생략).
MAX_ROWS_PER_FETCH = int(os.environ.get("POWER_PREDICTION_MAX_ROWS", 20_000))
PAGE_SIZE = 1000


def get_supabase_client() -> Optional[Client]:
    """main.py와 동일한 우선순위: SUPABASE_SERVICE_KEY(RLS 우회) > SUPABASE_KEY."""
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        return None
    try:
        return create_client(url, key)
    except Exception as e:
        print("[predict_tax_risk] Supabase 연결 실패:", e)
        return None


@dataclass
class MonthWindow:
    tz: ZoneInfo
    now_local: datetime
    month_start_local: datetime
    days_in_month: int
    hours_elapsed_today: float


def _month_window(tz_name: str = TZ_NAME) -> MonthWindow:
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    month_start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_in_month = monthrange(now_local.year, now_local.month)[1]
    hours_elapsed_today = (now_local - now_local.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 3600.0
    return MonthWindow(tz, now_local, month_start_local, days_in_month, hours_elapsed_today)


def _fetch_month_rows(client: Client, window: MonthWindow) -> list[dict]:
    """이번 달, 활성 포트에 한해 (created_at, power, port_number)만 최근 MAX_ROWS_PER_FETCH개 페이지네이션으로 조회."""
    start_utc_iso = window.month_start_local.astimezone(timezone.utc).isoformat()
    rows: list[dict] = []
    offset = 0
    while len(rows) < MAX_ROWS_PER_FETCH:
        resp = (
            client.table(TABLE_NAME)
            .select("created_at,power,port_number")
            .eq("device_id", DEVICE_ID)
            .in_("port_number", ACTIVE_PORTS)
            .gte("created_at", start_utc_iso)
            .order("created_at", desc=False)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows[:MAX_ROWS_PER_FETCH]


def _daily_avg_power_mw(rows: list[dict], window: MonthWindow) -> dict[int, float]:
    """day-of-month(1..) -> 그날 활성 포트 합산 평균전력(원시 mW, 환산 전). 여러 포트의 타임스탬프가
    정확히 안 맞아도, '하루 평균전력 × 경과시간'은 개별 샘플 사다리꼴적분과 등가라 이 방식을 씀."""
    per_day_port_sum: dict[tuple[int, int], list[float]] = {}
    for row in rows:
        try:
            ts = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")).astimezone(window.tz)
            power = float(row.get("power") or 0.0)
            port = int(row.get("port_number") or 0)
        except (KeyError, ValueError, TypeError):
            continue
        key = (ts.day, port)
        per_day_port_sum.setdefault(key, []).append(power)

    # 포트별 하루 평균 → 활성 포트 전체 합산
    day_totals: dict[int, float] = {}
    for (day, _port), values in per_day_port_sum.items():
        day_totals[day] = day_totals.get(day, 0.0) + (sum(values) / len(values))
    return day_totals


def _scaled_watts(raw_mw: float, factor: float) -> float:
    """실측 mW를 220V 가전 스케일 W로 환산."""
    return raw_mw * factor / 1000.0


def _resolve_extrapolation_factor(raw_total_so_far: float, raw_daily_rate: float, days_remaining: int) -> tuple[float, str]:
    """환산 배율 결정. 수동 오버라이드가 있으면 그걸 쓰고, 아니면 '지금까지 실측된 만큼 +
    남은 날짜를 지금 추세로 채웠을 때' 월말 예상치가 TARGET_MONTH_TOTAL_WH에 오도록 역산 (auto).
    (달의 앞부분 데이터가 아예 없어도 실제로 계산되는 예측치 공식과 정확히 같은 구조라 항상 일관됨.)
    투영값이 0 이하(데이터 전무)면 LOAD_EXTRAPOLATION_FALLBACK_FACTOR로 폴백."""
    if LOAD_EXTRAPOLATION_FACTOR_ENV not in ("", "auto"):
        try:
            return float(LOAD_EXTRAPOLATION_FACTOR_ENV), "manual"
        except ValueError:
            pass  # 잘못된 값이면 auto로 폴백

    raw_projected_total = raw_total_so_far + raw_daily_rate * days_remaining
    if raw_projected_total <= 0:
        return LOAD_EXTRAPOLATION_FALLBACK_FACTOR, "fallback(no-data)"

    factor = TARGET_MONTH_TOTAL_WH / raw_projected_total * 1000.0
    return factor, "auto"


def calc_monthly_fee_won(total_wh: float) -> float:
    """실제 한국 주택용 누진제와 동일한 구조(구간별 한계요금 누적 + 최고구간 기본요금)로 요금 계산.
    입력(total_wh)은 축소된 실측 기준(BRACKET_2/3_WH)의 값이므로, 여기서만 220V-등가로
    되돌려서(×220/12) 실제 한전 200kWh/400kWh 구간·실제 원/kWh 단가에 대입 — 그래야 요금이
    실제 가정 전기요금처럼 체감되는 크기(2구간 근처면 2~3만원대)로 나옴 (2026-07-30 확정)."""
    equiv_wh = total_wh / VOLTAGE_RATIO

    tier = 1 if equiv_wh <= FEE_TIER1_MAX_WH else (2 if equiv_wh <= FEE_TIER2_MAX_WH else 3)
    remaining = equiv_wh
    energy_charge = 0.0

    t1 = min(remaining, FEE_TIER1_MAX_WH)
    energy_charge += t1 * TIER_RATE_WON_PER_WH[1]
    remaining -= t1

    if remaining > 0:
        t2 = min(remaining, FEE_TIER2_MAX_WH - FEE_TIER1_MAX_WH)
        energy_charge += t2 * TIER_RATE_WON_PER_WH[2]
        remaining -= t2

    if remaining > 0:
        energy_charge += remaining * TIER_RATE_WON_PER_WH[3]

    return round(TIER_BASE_FEE_WON[tier] + energy_charge, 1)


def _run_monte_carlo(total_so_far_wh: float, daily_wh_actual: list[float], days_remaining: int,
                      today_rate_wh_per_day: float) -> list[float]:
    """부트스트랩 몬테카를로: 실측(환산 후) 일별 사용량 풀에서 남은 날짜만큼 복원추출하여 합산.
    데이터가 부족(2일 미만)하면 오늘 하루치 비율을 로그정규 노이즈로 흔들어 대체."""
    sims = []
    have_history = len(daily_wh_actual) >= 2
    for _ in range(MC_SIMULATIONS):
        if days_remaining <= 0:
            sims.append(total_so_far_wh)
            continue
        if have_history:
            remaining_total = sum(random.choice(daily_wh_actual) for _ in range(days_remaining))
        else:
            base_rate = today_rate_wh_per_day if today_rate_wh_per_day > 0 else 0.0
            remaining_total = sum(
                max(0.0, base_rate * random.lognormvariate(0, FALLBACK_NOISE_STD))
                for _ in range(days_remaining)
            )
        sims.append(total_so_far_wh + remaining_total)
    return sims


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, round(pct / 100 * (len(sorted_values) - 1))))
    return sorted_values[idx]


def _gemini_forecast(daily_wh_actual: list[float], today_so_far_wh: float, hours_elapsed_today: float,
                      days_remaining: int, days_in_month: int) -> Optional[dict]:
    """Gemini에게 월말 예상 사용량/신뢰구간/구간초과확률을 직접 추정하게 함.
    실패(키 없음/SDK 없음/호출 에러/스키마 안 맞음)하면 None을 반환해서 호출부가 몬테카를로로 폴백하게 함."""
    if genai is None or not GEMINI_API_KEY:
        return None
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
        너는 12V 소형 스마트 멀티탭의 이번 달 전력 사용량을 예측하는 AI야.
        모든 수치는 이미 220V 가정용 가전 스케일로 환산된 Wh(와트시) 단위야.

        - 이번 달 완결된 날짜별 실측 사용량(Wh, 날짜 순): {[round(v, 2) for v in daily_wh_actual]}
        - 오늘(진행 중인 날) 지금까지 사용량: {today_so_far_wh:.2f}Wh (하루 24시간 중 {hours_elapsed_today:.1f}시간 경과)
        - 남은 날짜 수: {days_remaining}일 (이번 달 총 {days_in_month}일)
        - 커스텀 누진세 구간: 2구간 임계값 {BRACKET_2_WH:g}Wh, 3구간 임계값 {BRACKET_3_WH:g}Wh

        위 데이터를 바탕으로 지금까지의 추세와 변동성을 감안해서:
        1. 월말 최종 예상 총 사용량(expected_total_wh)
        2. 불확실성을 반영한 하위 10%(p10_wh)/상위 90%(p90_wh) 구간 (p10 <= expected_total_wh <= p90 이어야 함)
        3. 월말까지 2구간/3구간 임계값을 초과할 확률(%, 0~100)
        4. 판단 근거를 한국어 딱 한 문장으로만 (짧게)

        을 추정해줘.
        """
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2048,
                response_mime_type="application/json",
                response_schema=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "expected_total_wh": genai_types.Schema(type=genai_types.Type.NUMBER),
                        "p10_wh": genai_types.Schema(type=genai_types.Type.NUMBER),
                        "p90_wh": genai_types.Schema(type=genai_types.Type.NUMBER),
                        "bracket2_probability_percent": genai_types.Schema(type=genai_types.Type.NUMBER),
                        "bracket3_probability_percent": genai_types.Schema(type=genai_types.Type.NUMBER),
                        "reasoning": genai_types.Schema(type=genai_types.Type.STRING),
                    },
                    required=[
                        "expected_total_wh", "p10_wh", "p90_wh",
                        "bracket2_probability_percent", "bracket3_probability_percent", "reasoning",
                    ],
                ),
            ),
        )
        import json
        result = json.loads(response.text)

        # 최소한의 sanity check - 이상한 값(음수, p10>p90 등)이면 폴백시킴
        if not (0 <= result["p10_wh"] <= result["expected_total_wh"] <= result["p90_wh"]):
            print("[predict_tax_risk] Gemini 응답이 비상식적(p10<=expected<=p90 위반) - 몬테카를로로 폴백")
            return None
        for key in ("bracket2_probability_percent", "bracket3_probability_percent"):
            result[key] = max(0.0, min(100.0, result[key]))
        return result
    except Exception as e:
        print("[predict_tax_risk] Gemini 예측 호출 실패, 몬테카를로로 폴백:", e)
        return None


_MC_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_monte_carlo_simulation",
        "description": (
            "실측 일별 사용량(Wh) 데이터를 부트스트랩 몬테카를로 시뮬레이션(5000회)에 돌려서, "
            "월말 예상 사용량의 p10/p50/p90 신뢰구간과 누진세 2/3구간 초과 확률(%)을 실제로 계산한다. "
            "이게 통계적으로 정확한 값이므로, 최종 답변의 숫자는 반드시 이 도구의 결과를 그대로 써야 한다 "
            "(직접 암산/추측하지 말 것)."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _run_monte_carlo_tool(total_so_far_wh: float, daily_wh_actual: list[float], days_remaining: int,
                           today_rate_wh_per_day: float) -> dict:
    """도구 호출 시 실제로 실행되는 함수 - 기존 _run_monte_carlo를 그대로 재사용해서 백분위/구간확률만 정리해 반환."""
    sims = sorted(_run_monte_carlo(total_so_far_wh, daily_wh_actual, days_remaining, today_rate_wh_per_day))
    prob2 = (sum(1 for s in sims if s > BRACKET_2_WH) / len(sims) * 100) if sims else 0.0
    prob3 = (sum(1 for s in sims if s > BRACKET_3_WH) / len(sims) * 100) if sims else 0.0
    return {
        "p10_wh": round(_percentile(sims, 10), 2),
        "p50_wh": round(_percentile(sims, 50), 2),
        "p90_wh": round(_percentile(sims, 90), 2),
        "bracket2_probability_percent": round(prob2, 1),
        "bracket3_probability_percent": round(prob3, 1),
        "simulations_run": len(sims),
    }


def _groq_forecast(daily_wh_actual: list[float], today_so_far_wh: float, hours_elapsed_today: float,
                    days_remaining: int, days_in_month: int, total_so_far_wh: float,
                    today_rate_wh_per_day: float) -> Optional[dict]:
    """Groq(Llama 계열, 함수 호출 지원)에게 도구(run_monte_carlo_simulation)를 쥐어주고 직접 호출시킨다.
    LLM이 확률/신뢰구간을 암산으로 추측하는 대신, 실제 몬테카를로 시뮬레이션 결과를 받아서 그 숫자를
    그대로 최종 답변에 쓰게 강제 - 판단 근거(자연어)만 AI가 생성, 숫자는 진짜 통계 계산 결과.
    실패하면 None을 반환해서 호출부가 몬테카를로 단독 실행으로 폴백하게 함."""
    if Groq is None or not GROQ_API_KEY:
        return None
    try:
        client = Groq(api_key=GROQ_API_KEY)
        system_prompt = (
            "너는 12V 소형 스마트 멀티탭의 이번 달 전력 사용량을 예측하는 AI야. "
            "모든 수치는 이미 220V 가정용 가전 스케일로 환산된 Wh 단위야. "
            "정확한 확률/신뢰구간은 절대 암산하지 말고, 반드시 run_monte_carlo_simulation 도구를 호출해서 "
            "실제 통계 시뮬레이션 결과를 받은 뒤, 그 숫자를 그대로 사용해서 최종 답변을 작성해."
        )
        user_prompt = f"""
        - 이번 달 완결된 날짜별 실측 사용량(Wh, 날짜 순): {[round(v, 2) for v in daily_wh_actual]}
        - 오늘(진행 중인 날) 지금까지 사용량: {today_so_far_wh:.2f}Wh (하루 24시간 중 {hours_elapsed_today:.1f}시간 경과)
        - 남은 날짜 수: {days_remaining}일 (이번 달 총 {days_in_month}일)
        - 커스텀 누진세 구간: 2구간 임계값 {BRACKET_2_WH:g}Wh, 3구간 임계값 {BRACKET_3_WH:g}Wh

        run_monte_carlo_simulation 도구를 호출해서 정확한 시뮬레이션 결과를 받아와.
        """

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        # Llama 계열 모델이 가끔 함수 호출을 정식 tool_call 대신 텍스트로 흘려써서
        # Groq 서버가 "tool_use_failed" 400을 반환하는 경우가 있음 (모델 특성, 일관적이지 않음).
        # 흔한 일회성 실패라 재시도(최대 3번, temperature 0으로 낮춰서)로 대부분 해결됨.
        msg = None
        last_tool_error = None
        for attempt in range(3):
            try:
                first = client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=messages,
                    tools=[_MC_TOOL_SCHEMA],
                    tool_choice={"type": "function", "function": {"name": "run_monte_carlo_simulation"}},
                    temperature=0.0,
                )
                msg = first.choices[0].message
                break
            except Exception as e:
                last_tool_error = e
                print(f"[predict_tax_risk] Groq 도구 호출 {attempt + 1}회차 실패, 재시도:", e)
        if msg is None:
            print("[predict_tax_risk] Groq 도구 호출 3회 모두 실패 - 몬테카를로로 폴백:", last_tool_error)
            return None

        tool_calls = msg.tool_calls or []
        if not tool_calls:
            print("[predict_tax_risk] Groq가 도구를 호출하지 않음 - 몬테카를로로 폴백")
            return None

        sim_result = _run_monte_carlo_tool(total_so_far_wh, daily_wh_actual, days_remaining, today_rate_wh_per_day)

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(sim_result, ensure_ascii=False),
            })

        messages.append({
            "role": "user",
            "content": (
                "이제 위 시뮬레이션 결과를 그대로 사용해서 최종 답변을 JSON으로만 출력해 (다른 텍스트 없이). "
                '형식: {"expected_total_wh": 숫자, "p10_wh": 숫자, "p90_wh": 숫자, '
                '"bracket2_probability_percent": 숫자, "bracket3_probability_percent": 숫자, '
                '"reasoning": "한국어 한 문장, 시뮬레이션 결과를 짧게 설명"}. '
                "expected_total_wh/p10_wh/p90_wh/두 확률값은 시뮬레이션 결과(p50_wh 등)를 그대로 옮겨 적어, 바꾸지 마."
            ),
        })

        second = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        result = json.loads(second.choices[0].message.content)

        # 안전장치: 모델이 숫자를 잘못 옮겨적었거나 형식이 이상하면 시뮬레이션 결과값으로 강제 대체
        # (판단 근거 문장만 모델 것을 쓰고, 숫자는 항상 진짜 계산 결과가 보장되게)
        if not (0 <= result.get("p10_wh", -1) <= result.get("expected_total_wh", -1) <= result.get("p90_wh", -1)):
            print("[predict_tax_risk] Groq 응답 숫자가 비상식적 - 시뮬레이션 결과로 강제 대체")
            result["expected_total_wh"] = sim_result["p50_wh"]
            result["p10_wh"] = sim_result["p10_wh"]
            result["p90_wh"] = sim_result["p90_wh"]
        for key in ("bracket2_probability_percent", "bracket3_probability_percent"):
            result[key] = max(0.0, min(100.0, result.get(key, sim_result[key])))
        if "reasoning" not in result:
            result["reasoning"] = "몬테카를로 시뮬레이션 결과 기반 예측."
        return result
    except Exception as e:
        print("[predict_tax_risk] Groq 예측 호출 실패, 몬테카를로로 폴백:", e)
        return None


def predict_monthly_tax_risk() -> dict:
    window = _month_window()
    client = get_supabase_client()

    if client is None:
        return {
            "error": "Supabase 연결 정보(SUPABASE_URL / SUPABASE_SERVICE_KEY 또는 SUPABASE_KEY)가 없습니다.",
        }

    rows = _fetch_month_rows(client, window)
    day_totals_mw_avg = _daily_avg_power_mw(rows, window)

    # 배율 계산은 환산 전(raw mW) 값 기준으로 먼저 해야 함 (자기참조 방지)
    raw_full_days = [
        avg_mw * 24.0 for day, avg_mw in day_totals_mw_avg.items() if day < window.now_local.day
    ]
    today_avg_mw = day_totals_mw_avg.get(window.now_local.day, 0.0)
    raw_today_so_far = today_avg_mw * max(window.hours_elapsed_today, 0.0)
    raw_today_rate = (
        today_avg_mw * 24.0 if window.hours_elapsed_today <= 0 else
        (raw_today_so_far / window.hours_elapsed_today) * 24.0
    )
    raw_daily_rate_estimate = statistics.mean(raw_full_days) if raw_full_days else raw_today_rate
    raw_total_so_far = sum(raw_full_days) + raw_today_so_far
    days_remaining = window.days_in_month - window.now_local.day  # 오늘 이후 남은 '완결될' 날 수

    factor, factor_source = _resolve_extrapolation_factor(raw_total_so_far, raw_daily_rate_estimate, days_remaining)

    # 완결된 날짜들의 Wh: raw_full_days는 이미 'avg_mw × 24h' 단위라 factor/1000만 곱하면 됨
    daily_wh_actual = [v * factor / 1000.0 for v in raw_full_days]

    today_scaled_w = _scaled_watts(today_avg_mw, factor)
    today_so_far_wh = today_scaled_w * max(window.hours_elapsed_today, 0.0)
    today_rate_wh_per_day = (
        today_scaled_w * 24.0 if window.hours_elapsed_today <= 0 else
        (today_so_far_wh / window.hours_elapsed_today) * 24.0
    )

    total_so_far_wh = sum(daily_wh_actual) + today_so_far_wh

    ai_reasoning = None
    engine_used = "montecarlo"
    cache_age_seconds = None

    if days_remaining <= 0:
        # 이미 월말 - 예측할 미래가 없으니 Gemini 호출 없이 확정값 사용
        p10_wh = p50_wh = p90_wh = total_so_far_wh
        prob_bracket2 = 100.0 if total_so_far_wh > BRACKET_2_WH else 0.0
        prob_bracket3 = 100.0 if total_so_far_wh > BRACKET_3_WH else 0.0
        engine_used = "deterministic(month-end)"
    else:
        ai_result = None
        cache_ttl = GROQ_CACHE_TTL_SECONDS if PREDICTION_ENGINE == "groq" else GEMINI_CACHE_TTL_SECONDS

        if PREDICTION_ENGINE in ("groq", "gemini"):
            now_ts = time.time()
            cache_fresh = (
                _ai_cache["result"] is not None
                and _ai_cache.get("engine") == PREDICTION_ENGINE
                and (now_ts - _ai_cache["computed_at"]) < cache_ttl
            )
            if cache_fresh:
                ai_result = _ai_cache["result"]
                cache_age_seconds = round(now_ts - _ai_cache["computed_at"])
            else:
                if PREDICTION_ENGINE == "groq":
                    ai_result = _groq_forecast(
                        daily_wh_actual, today_so_far_wh, window.hours_elapsed_today, days_remaining,
                        window.days_in_month, total_so_far_wh, today_rate_wh_per_day,
                    )
                else:
                    ai_result = _gemini_forecast(
                        daily_wh_actual, today_so_far_wh, window.hours_elapsed_today, days_remaining, window.days_in_month,
                    )
                if ai_result is not None:
                    _ai_cache["result"] = ai_result
                    _ai_cache["computed_at"] = now_ts
                    _ai_cache["engine"] = PREDICTION_ENGINE
                    cache_age_seconds = 0

        if ai_result is not None:
            p10_wh = ai_result["p10_wh"]
            p50_wh = ai_result["expected_total_wh"]
            p90_wh = ai_result["p90_wh"]
            prob_bracket2 = ai_result["bracket2_probability_percent"]
            prob_bracket3 = ai_result["bracket3_probability_percent"]
            ai_reasoning = ai_result["reasoning"]
            engine_used = f"{PREDICTION_ENGINE}(cached)" if cache_age_seconds else f"{PREDICTION_ENGINE}(live)"
        else:
            sims = sorted(_run_monte_carlo(total_so_far_wh, daily_wh_actual, days_remaining, today_rate_wh_per_day))
            p10_wh = _percentile(sims, 10)
            p50_wh = _percentile(sims, 50)
            p90_wh = _percentile(sims, 90)
            prob_bracket2 = (sum(1 for s in sims if s > BRACKET_2_WH) / len(sims) * 100) if sims else 0.0
            prob_bracket3 = (sum(1 for s in sims if s > BRACKET_3_WH) / len(sims) * 100) if sims else 0.0
            engine_used = "montecarlo-fallback" if PREDICTION_ENGINE in ("groq", "gemini") else "montecarlo"

    expected_wh = p50_wh

    # 트렌드 그래프용: 실측 누적(1일~오늘) + 예측 누적(내일~월말, median/p10/p90 일평균으로 선형 전개)
    trend = []
    cumulative = 0.0
    for day in range(1, window.now_local.day):
        cumulative += _scaled_watts(day_totals_mw_avg.get(day, 0.0), factor) * 24.0
        trend.append({
            "day": day,
            "type": "actual",
            "cumulative_wh": round(cumulative, 2),
        })
    cumulative += today_so_far_wh
    trend.append({
        "day": window.now_local.day,
        "type": "actual",
        "cumulative_wh": round(cumulative, 2),
    })

    if days_remaining > 0:
        # 남은 날짜에 걸쳐 지금 누적치(cumulative)에서 최종 예측치(p10/50/90_wh)까지 선형 보간.
        # (일평균 사용량을 그대로 우려먹는 대신 이렇게 해야, 어느 예측 엔진을 쓰든 그래프 끝점이
        # 상단 통계 카드에 표시되는 예상치와 항상 정확히 일치함 - Gemini 값은 몬테카를로의 단순
        # 평균-외삽과 다를 수 있어서 이 보정이 없으면 카드와 그래프가 서로 다른 숫자를 보여줌)
        median_daily_rate = (p50_wh - cumulative) / days_remaining
        p10_daily = (p10_wh - cumulative) / days_remaining
        p90_daily = (p90_wh - cumulative) / days_remaining
        proj_mid = proj_p10 = proj_p90 = cumulative
        for day in range(window.now_local.day + 1, window.days_in_month + 1):
            proj_mid += median_daily_rate
            proj_p10 += p10_daily
            proj_p90 += p90_daily
            trend.append({
                "day": day,
                "type": "projected",
                "cumulative_wh": round(proj_mid, 2),
                "p10_wh": round(proj_p10, 2),
                "p90_wh": round(proj_p90, 2),
            })

    return {
        "period": {
            "month": window.now_local.strftime("%Y-%m"),
            "days_in_month": window.days_in_month,
            "today": window.now_local.day,
            "days_remaining": max(days_remaining, 0),
            "generated_at": window.now_local.isoformat(),
        },
        "current": {
            "cumulative_wh": round(total_so_far_wh, 2),
            "active_ports": ACTIVE_PORTS,
        },
        "prediction": {
            "expected_total_wh": round(expected_wh, 2),
            "confidence_interval_wh": {"p10": round(p10_wh, 2), "p50": round(expected_wh, 2), "p90": round(p90_wh, 2)},
            "bracket_probabilities_percent": {
                "bracket2_over_200wh": round(prob_bracket2, 1),
                "bracket3_over_400wh": round(prob_bracket3, 1),
            },
            "expected_monthly_fee_won": calc_monthly_fee_won(expected_wh),
            "fee_range_won": {
                "p10": calc_monthly_fee_won(p10_wh),
                "p90": calc_monthly_fee_won(p90_wh),
            },
            "engine": engine_used,
            "ai_reasoning": ai_reasoning,
        },
        "trend": trend,
        "brackets": {
            "tier1_max_wh": TIER1_MAX_WH,
            "tier2_max_wh": TIER2_MAX_WH,
        },
        "meta": {
            "table": TABLE_NAME,
            "device_id": DEVICE_ID,
            "active_ports": ACTIVE_PORTS,
            "rows_used": len(rows),
            "days_with_data": len(daily_wh_actual) + (1 if today_avg_mw else 0),
            "load_extrapolation_factor": round(factor, 2),
            "load_extrapolation_factor_source": factor_source,
            "prediction_engine": engine_used,
            "gemini_cache_age_seconds": cache_age_seconds,
            "gemini_cache_ttl_seconds": GEMINI_CACHE_TTL_SECONDS,
            "note": (
                f"예측 엔진: {engine_used}. 사용량(누적/예측/게이지 구간)은 실측값 그대로(배율 {factor:.2f}·{factor_source}, "
                f"기본 1.0=raw) — 구간 임계값(2구간 {BRACKET_2_WH:.1f}Wh/3구간 {BRACKET_3_WH:.1f}Wh)도 원래 220V 기준"
                f"(200/400Wh)에서 12V/220V 전압비({VOLTAGE_RATIO:.4f})만큼 낮춘 값이라 실측 규모에 자연스럽게 맞음. "
                "요금(원)만 별도로, 이 축소된 사용량을 다시 220V-등가로 환산해서(×220/12) 실제 한전 200kWh/400kWh "
                "구간·실제 원/kWh 단가에 대입 — 그래야 2구간 근처에서 실제 가정 전기요금처럼 체감되는 2~3만원대가 "
                "나옴 (기본요금 TIER_BASE_FEE_WON, 단가 TIER_RATE_WON_PER_WH 둘 다 실제 한전 고시요금 기반)."
            ),
        },
    }


if __name__ == "__main__":
    import json
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 기본 cp949라 em-dash 등이 깨짐
    print(json.dumps(predict_monthly_tax_risk(), ensure_ascii=False, indent=2))
