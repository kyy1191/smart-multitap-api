from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import time

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

# [API 1] 실시간 상태 가져오기
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

# [API 2] 수동 제어
@app.post("/control")
def control_port(req: ControlData):
    state = get_state()
    state["power"][str(req.port_number)] = req.is_on
    if req.is_on:
        state["last_toggle_time"][str(req.port_number)] = time.time()
    save_state(state)
    return {"message": "제어 성공"}

# [API 3] 센서 데이터 업로드 & 방어 로직 (딜레이 없음!)
@app.post("/upload-data")
def upload_data(data: PowerData):
    state = get_state()
    p_str = str(data.port_number)
    wifi_connected = state["wifi"]
    port_type = state["types"].get(p_str, "일반")
    just_turned_on = (time.time() - state["last_toggle_time"].get(p_str, 0)) < 5 

    if not wifi_connected and port_type == "위험":
        state["power"][p_str] = False

    if port_type == "상시":
        data.is_on = True
        state["power"][p_str] = True 
        data.action_reason = "상시기기 (항시 작동)"
    elif state["power"].get(p_str) == False:
        data.is_on = False
        data.voltage = 0.0; data.current = 0.0; data.power = 0.0
        data.action_reason = "차단 상태"
    elif just_turned_on:
        data.is_on = True
        data.action_reason = "기기 부팅 중 (5초 유예)"
    else:
        if data.temperature >= 80.0:
            data.is_on = False
            data.action_reason = "과열 차단"
            state["power"][p_str] = False
        else:
            data.action_reason = "정상 작동"
            
    save_state(state)
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공"}

# [API 4] ⭐️ 새롭게 추가된 대시보드 통계 전용 API
@app.get("/get-stats")
def get_stats():
    # 1. Supabase에서 기기가 꺼진(is_on=False) 최신 로그를 가져옵니다.
    response = supabase.table("sensor_data").select("action_reason").eq("is_on", False).limit(500).execute()
    
    # 2. 화재 예방(자동 차단) 횟수 계산
    # 사유에 '차단'이라는 단어가 포함된 로그의 개수를 셉니다.
    cut_count = 0
    if response.data:
        cut_count = len([row for row in response.data if "차단" in row.get("action_reason", "")])

    # 3. 전력량(kWh) 및 절약 비용(원) 계산
    # * 1회 차단될 때마다 대략 0.4 kWh를 아꼈다고 가정 (원하시는 수식으로 변경 가능)
    # * 1 kWh당 전기요금을 150원으로 가정
    saved_kwh_today = cut_count * 0.4 
    saved_cost_today = int(saved_kwh_today * 150)
    
    # 누적 성과 (기본 베이스 숫자에 오늘의 성과를 더함)
    cumulative_kwh = saved_kwh_today + 124.0
    cumulative_cost = int(cumulative_kwh * 150)

    # 4. 앱(프론트엔드)에서 쓰기 좋게 딕셔너리로 묶어서 응답
    return {
        "today": {
            "energy_saved_kwh": round(saved_kwh_today, 1),
            "cost_saved_won": saved_cost_today,
            "fire_prevention_count": cut_count
        },
        "cumulative": {
            "total_kwh_saved": round(cumulative_kwh, 1),
            "total_cost_saved": cumulative_cost
        }
    }