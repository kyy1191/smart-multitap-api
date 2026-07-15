from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
gpt_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# ⭐️ [핵심 1] 서버의 메모리 장부! (한 번 꺼지면 잠가버리기 위함)
# True면 켜짐(허용), False면 꺼짐(완전 차단)
port_power_state = {
    1: True,
    2: True,
    3: True,
    4: True
}

class PowerData(BaseModel):
    room: str
    device_id: str
    port_number: int
    voltage: float
    current: float
    power: float
    temperature: float
    is_on: bool
    action_reason: str

# 프론트엔드 수동 조작용 데이터 구조
class ControlData(BaseModel):
    port_number: int
    is_on: bool

@app.get("/get-data")
def get_data():
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(4).execute()
    return response.data

@app.get("/esg-report")
def get_esg_report():
    try:
        response = supabase.table("sensor_data").select("*").like("action_reason", "%차단%").execute()
        cut_count = len(response.data)
        saved_kwh = round(cut_count * 0.36, 1) 
        saved_money = int(saved_kwh * 430) 
        saved_carbon = round(saved_kwh * 0.478, 1) 
        return {"cut_count": cut_count, "saved_kwh": saved_kwh, "saved_money": saved_money, "saved_carbon": saved_carbon}
    except Exception as e:
        return {"error": str(e)}

# ⭐️ [핵심 2] 앱에서 초록 버튼 누를 때 호출될 '원격 제어' 주소
@app.post("/control")
def control_port(req: ControlData):
    # 앱에서 보낸 명령대로 서버 메모리 장부를 바꿈! (다시 켜주거나, 수동으로 끄거나)
    port_power_state[req.port_number] = req.is_on
    state_str = "켜짐" if req.is_on else "꺼짐"
    return {"message": f"포트 {req.port_number} 전원 {state_str} 상태로 앱에서 수동 변경 완료!"}

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
    # 1. 먼저 메모리 장부를 확인! (수동으로 껐거나, 과열로 한 번 차단된 적이 있다면?)
    if port_power_state[data.port_number] == False:
        # 센서가 무슨 값을 보내든 싹 다 강제로 무시하고 0으로 만들어버림!
        data.is_on = False
        data.voltage = 0.0
        data.current = 0.0
        data.power = 0.0
        data.action_reason = "차단 유지 중 (앱에서 다시 켜기 대기)"
    
    # 2. 전원이 켜져 있을 때만 위험 감지 로직 수행
    else:
        if data.temperature >= 80.0:
            data.is_on = False
            data.action_reason = f"포트 {data.port_number} 과열 감지: 즉시 강제 차단"
            # ⭐️ 위험해서 차단했으니 메모리 장부도 False로 잠가버림!
            port_power_state[data.port_number] = False 
        elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
            data.is_on = False
            data.action_reason = f"AI 판단: 포트 {data.port_number} 위험 및 낭비 차단"
            port_power_state[data.port_number] = False
        else:
            data.action_reason = "정상 작동"
    
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공", "result": data}