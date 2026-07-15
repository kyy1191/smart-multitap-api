from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import time  # ⭐️ 시간 측정을 위해 추가!
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

STATE_FILE = "/tmp/smart_state.json" # Render 임시 폴더 사용

def get_state():
    if not os.path.exists(STATE_FILE):
        initial = {
            "power": {"1": True, "2": True, "3": True, "4": True},
            "wifi": True,
            "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"},
            "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0} # ⭐️ 켤 때마다 시간 기록
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

# ... (중략: PowerData, ControlData 클래스 등은 동일)

@app.post("/control")
def control_port(req: ControlData):
    state = get_state()
    state["power"][str(req.port_number)] = req.is_on
    if req.is_on: # ⭐️ 켜는 순간 시간 기록!
        state["last_toggle_time"][str(req.port_number)] = time.time()
    save_state(state)
    return {"message": "제어 성공"}

# ... (중략: 다른 API들은 그대로 두면 됨)

@app.post("/upload-data")
def upload_data(data: PowerData):
    state = get_state()
    p_str = str(data.port_number)
    
    wifi_connected = state["wifi"]
    port_type = state["types"].get(p_str, "일반")

    # ⭐️ 5초 예외 시간 로직 (시동 지연 방지)
    just_turned_on = (time.time() - state["last_toggle_time"].get(p_str, 0)) < 5 

    if not wifi_connected and port_type == "위험":
        state["power"][p_str] = False

    if state["power"].get(p_str) == False:
        data.is_on = False
        data.voltage = 0.0
        data.current = 0.0
        data.power = 0.0
        data.action_reason = "차단 상태"
            
    # ⭐️ 켜진 상태면서 + 5초가 안 지났으면 안전장치 검사 통과!
    elif just_turned_on:
        data.is_on = True
        data.action_reason = "기기 부팅 중 (안전장치 5초 유예)"
        
    else:
        # 정상적인 안전장치 검사 구간
        if data.temperature >= 80.0:
            data.is_on = False
            data.action_reason = f"포트 {data.port_number} 과열 감지: 차단"
            state["power"][p_str] = False
        elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
            data.is_on = False
            data.action_reason = f"AI 판단: 위험 차단"
            state["power"][p_str] = False
        else:
            data.action_reason = "정상 작동"
    
    save_state(state)
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공"}