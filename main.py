from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
from openai import OpenAI

app = FastAPI()

# 1. CORS 설정 (친구 웹사이트가 접속할 수 있게 해주는 방어막 해제)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Supabase 설정 (환경 변수 사용)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 3. GPT 설정 (환경 변수 사용)
gpt_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ⭐️ 4. 데이터 구조 정의 (port_number 항목 추가!)
class PowerData(BaseModel):
    device_id: str
    room: str 
    port_number: int  # 👈 P1, P2, P3, P4 포트를 구분하기 위한 숫자형 변수 추가!
    voltage: float
    current: float
    power: float
    temperature: float
    is_on: bool
    action_reason: str

# ⭐️ 5. 친구 웹사이트를 위한 GET (최근 4개 포트 데이터 한 번에 주기)
@app.get("/get-data")
def get_data():
    # Supabase에서 가장 최근에 들어온 데이터 4개를 순서대로 가져옴
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(4).execute()
    return response.data

# 6. GPT 차단 여부 판단 함수
def ask_gpt_to_cut_power(voltage, current, power, temperature):
    prompt = f"현재 센서값: 전압{voltage}V, 전류{current}A, 전력{power}W, 온도{temperature}도. 위험하거나 낭비면 'CUT', 아니면 'KEEP'이라고만 답해."
    try:
        response = gpt_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.1
        )
        return response.choices[0].message.content.strip() == "CUT"
    except:
        return False

# 7. 라즈베리파이(또는 테스트용 Swagger)로부터 데이터 받는 POST
@app.post("/upload-data")
def upload_data(data: PowerData):
    # 위험 감지 및 AI 판단 로직 수행
    if data.temperature >= 80.0:
        data.is_on = False
        data.action_reason = f"포트 {data.port_number} 과열 감지: 즉시 강제 차단"
    elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
        data.is_on = False
        data.action_reason = f"AI 판단: 포트 {data.port_number} 위험 및 낭비 감지 차단"
    else:
        data.action_reason = "정상 작동"
    
    # DB에 데이터 저장
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공", "result": data}
# ⭐️ 8. 이번 달 ESG 및 요금 절감 리포트 API
@app.get("/esg-report")
def get_esg_report():
    try:
        # 1. DB에서 AI가 차단한 기록만 싹 가져오기 (action_reason에 '차단'이 포함된 데이터)
        response = supabase.table("sensor_data").select("*").like("action_reason", "%차단%").execute()
        cut_data = response.data
        
        # 2. 차단 횟수 계산
        cut_count = len(cut_data)
        
        # 3. 절약 전력량(kWh) 및 요금, 탄소 감소량 계산 (임시 로직)
        # 실제로는 누적 시간을 곱해야 하지만, 메이커톤 시연용으로 차단 1회당 평균 0.36kWh를 아꼈다고 가정!
        saved_kwh = round(cut_count * 0.36, 1) 
        saved_money = int(saved_kwh * 430)  # 1kWh당 약 430원 (누진세 가정)
        saved_carbon = round(saved_kwh * 0.478, 1) # 1kWh당 탄소배출계수 약 0.478kg
        
        return {
            "cut_count": cut_count,          # 자동 차단 횟수
            "saved_kwh": saved_kwh,          # 절약 전력 (kWh)
            "saved_money": saved_money,      # 예상 절약 금액 (원)
            "saved_carbon": saved_carbon     # 탄소 배출 감소 (kg CO2)
        }
    except Exception as e:
        return {"error": str(e)}