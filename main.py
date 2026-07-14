from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
from openai import OpenAI

app = FastAPI()

# ⭐️ 1. CORS 설정 (친구 웹사이트가 접속할 수 있게 해주는 마법의 방어막 해제)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Supabase 설정 (원래 적혀있던 정보 그대로 유지!)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 3. GPT 설정
gpt_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

class PowerData(BaseModel):
    device_id: str
    voltage: float
    current: float
    power: float
    temperature: float
    is_on: bool
    action_reason: str

# ⭐️ 4. 친구 웹사이트를 위한 GET (데이터 주는 약속)
@app.get("/get-data")
def get_data():
    # Supabase에서 가장 최근 데이터 1개를 가져옴
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(1).execute()
    return response.data

# ⭐️ 5. 라즈베리파이를 위한 POST (데이터 받는 약속 + AI 판단)
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

@app.post("/upload-data")
def upload_data(data: PowerData):
    # 위험 감지 로직
    if data.temperature >= 80.0:
        data.is_on = False
        data.action_reason = "과열 감지: 강제 차단"
    elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
        data.is_on = False
        data.action_reason = "AI 판단: 위험 및 낭비 감지 차단"
    else:
        data.action_reason = "정상 작동"
    
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공", "result": data}