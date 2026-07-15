from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import time
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

# [핵심] 상태 장부 가져오기/저장하기
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

# ⭐️ 여기가 제일 중요해! mock_sensor.py가 보내는 데이터랑 똑같아야 함!
class PowerData(BaseModel):
    room: str
    device_id: str
    port_number: int
    voltage: float
    current: float
    power: float
    temperature: float
    is_on: bool
    action_reason: str # 👈 이 녀석이 꼭 있어야 해!

class ControlData(BaseModel):
    port_number: int
    is_on: bool

class WifiStatus(BaseModel):
    connected: bool

class PortTypeUpdate(BaseModel):
    port_number: int
    device_type: str

# [API들] ... (위에서 준 코드랑 동일하니까 그대로 유지)
@app.get("/get-data")
def get_data():
    state = get_state()
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(16).execute()
    latest_data = {}
    for row in response.data:
        p = row["port_number"]
        if p not in latest_data:
            latest_data[p] = row
    result = list(latest_data.values())
    for row in result:
        p_str = str(row["port_number"])
        row["device_type"] = state["types"].get(p_str, "일반")
        row["wifi_connected"] = state["wifi"]
        if not state["wifi"] and state["types"].get(p_str) == "위험":
            state["power"][p_str] = False
            save_state(state) 
        if state["power"].get(p_str) == False:
            row["is_on"] = False
            row["power"] = 0.0
            row["action_reason"] = "차단 상태"
    result.sort(key=lambda x: x["port_number"])
    return result

@app.post("/control")
def control_port(req: ControlData):
    state = get_state()
    state["power"][str(req.port_number)] = req.is_on
    if req.is_on:
        state["last_toggle_time"][str(req.port_number)] = time.time()
    save_state(state)
    return {"message": "제어 성공"}

@app.post("/upload-data")
def upload_data(data: PowerData):
    state = get_state()
    p_str = str(data.port_number)
    wifi_connected = state["wifi"]
    port_type = state["types"].get(p_str, "일반")
    
    # [수정] 5초 유예 시간 확인
    just_turned_on = (time.time() - state["last_toggle_time"].get(p_str, 0)) < 5 

    # 1. 위험기기 + 와이파이 끊김 -> 즉시 차단
    if not wifi_connected and port_type == "위험":
        state["power"][p_str] = False

    # 2. 강제 차단 상태인지 확인
    if state["power"].get(p_str) == False:
        data.is_on = False
        data.voltage = 0.0
        data.current = 0.0
        data.power = 0.0
        data.action_reason = "차단 상태"
            
    # 3. 부팅 중 5초 유예
    elif just_turned_on:
        data.is_on = True
        data.action_reason = "기기 부팅 중 (5초 유예)"
        
    # ⭐️ [핵심 추가] 상시기기는 AI/과열 체크를 건너뛰고 무조건 켜짐 유지!
    elif port_type == "상시":
        data.is_on = True
        data.action_reason = "상시기기 (항시 작동)"

    # 4. 그 외(일반/위험기기)는 정상적으로 AI 판단 수행
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