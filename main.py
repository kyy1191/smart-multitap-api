from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import time
import traceback
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

STATE_FILE = "/tmp/smart_state.json"

# [장부 시스템]
def get_state():
    if not os.path.exists(STATE_FILE):
        initial = {
            "power": {"1": True, "2": True, "3": True, "4": True},
            "wifi": True,
            "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"},
            "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0}
        }
        save_state(initial)
        return initial
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"power": {"1": True, "2": True, "3": True, "4": True}, "wifi": True, "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"}, "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# [AI 차단 판독기] - ⭐️ 예외 처리로 절대 안 뻗게 함
def ask_gpt_to_cut_power(voltage, current, power, temperature):
    try:
        prompt = f"현재 센서값: 전압{voltage}V, 전류{current}A, 전력{power}W, 온도{temperature}도. 위험하거나 낭비면 'CUT', 아니면 'KEEP'이라고만 답해."
        response = gpt_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.1
        )
        return response.choices[0].message.content.strip() == "CUT"
    except Exception as e:
        print(f"AI 호출 실패 (서버는 안 죽음): {e}")
        return False # AI가 대답 못하면 차단 안 함(안전)

# [데이터 구조]
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

# [API]
@app.get("/get-data")
def get_data():
    state = get_state()
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(16).execute()
    result = list({row["port_number"]: row for row in response.data}.values())
    
    for row in result:
        p_str = str(row["port_number"])
        row["device_type"] = state["types"].get(p_str, "일반")
        row["wifi_connected"] = state["wifi"]
        if state["power"].get(p_str) == False:
            row["is_on"] = False
            row["power"] = 0.0
            row["action_reason"] = "차단 상태"
    return sorted(result, key=lambda x: x["port_number"])

@app.post("/control")
def control_port(req: ControlData):
    state = get_state()
    p_str = str(req.port_number)
    state["power"][p_str] = req.is_on
    if req.is_on:
        state["last_toggle_time"][p_str] = time.time()
    save_state(state)
    return {"message": "제어 성공"}

@app.post("/upload-data")
def upload_data(data: PowerData):
    try:
        state = get_state()
        p_str = str(data.port_number)
        wifi_connected = state["wifi"]
        port_type = state["types"].get(p_str, "일반")
        just_turned_on = (time.time() - state["last_toggle_time"].get(p_str, 0)) < 5 

        # 1. 와이파이 끊김 & 위험기기
        if not wifi_connected and port_type == "위험":
            state["power"][p_str] = False

        # 2. 차단 상태 확인
        if state["power"].get(p_str) == False:
            data.is_on = False
            data.voltage = 0.0; data.current = 0.0; data.power = 0.0
            data.action_reason = "차단 상태"
            
        # 3. 부팅 유예
        elif just_turned_on:
            data.is_on = True
            data.action_reason = "기기 부팅 중 (5초 유예)"
            
        # 4. 상시기기 보호
        elif port_type == "상시":
            data.is_on = True
            data.action_reason = "상시기기 (항시 작동)"

        # 5. 일반/위험기기 AI 판단
        else:
            if data.temperature >= 80.0:
                data.is_on = False
                data.action_reason = "과열 차단"
                state["power"][p_str] = False
            elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
                data.is_on = False
                data.action_reason = "AI 판단: 위험 차단"
                state["power"][p_str] = False
            else:
                data.action_reason = "정상 작동"
        
        save_state(state)
        supabase.table("sensor_data").insert(data.dict()).execute()
        return {"message": "데이터 처리 성공"}
        
    except Exception as e:
        traceback.print_exc() # 에러 발생 시 로그 출력
        return {"error": "서버 처리 중 오류", "detail": str(e)}