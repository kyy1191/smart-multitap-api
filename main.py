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

# ⭐️ 서버 메모리 장부 (True: 켜짐, False: 차단됨)
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

class ControlData(BaseModel):
    port_number: int
    is_on: bool

# ⭐️ [수정된 핵심 1] 프론트엔드에 데이터 보내기 전에 상태 강제 덮어쓰기!
@app.get("/get-data")
def get_data():
    # 넉넉하게 8개를 가져와서 포트별 최신 데이터만 딱 걸러냄
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(8).execute()
    
    latest_data = {}
    for row in response.data:
        p = row["port_number"]
        if p not in latest_data:
            latest_data[p] = row
            
    result = list(latest_data.values())

    # ⭐️ 제일 중요한 방어 로직: DB가 최신이 아니더라도 메모리가 꺼져있으면 무조건 끈다!
    for row in result:
        p = row["port_number"]
        if port_power_state[p] == False:
            row["is_on"] = False
            row["power"] = 0.0
            row["action_reason"] = "차단 유지 중 (앱에서 켜기 대기)"
            
    return result

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

# 앱에서 수동으로 끄고 켤 때
@app.post("/control")
def control_port(req: ControlData):
    port_power_state[req.port_number] = req.is_on # 메모리 상태 변경
    state_str = "켜짐" if req.is_on else "꺼짐"
    return {"message": f"포트 {req.port_number} 전원 {state_str} 상태로 수동 변경 완료!"}

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

# 센서에서 데이터 들어올 때
@app.post("/upload-data")
def upload_data(data: PowerData):
    # 1. 수동이나 과열로 꺼진 상태라면 데이터 강제 무시
    if port_power_state[data.port_number] == False:
        data.is_on = False
        data.voltage = 0.0
        data.current = 0.0
        data.power = 0.0
        data.action_reason = "차단 유지 중 (앱에서 켜기 대기)"
        
    # 2. 켜져 있을 때만 위험 판단 로직 수행
    else:
        if data.temperature >= 80.0:
            data.is_on = False
            data.action_reason = f"포트 {data.port_number} 과열 감지: 즉시 강제 차단"
            port_power_state[data.port_number] = False # ⭐️ 위험해서 차단했으니 락(Lock) 걸기!
        elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
            data.is_on = False
            data.action_reason = f"AI 판단: 포트 {data.port_number} 위험 차단"
            port_power_state[data.port_number] = False
        else:
            data.action_reason = "정상 작동"
    
    # 조작이 끝난 데이터를 최종적으로 DB에 저장
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공", "result": data}