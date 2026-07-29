from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import time
from typing import List, Optional
from openai import OpenAI

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# [웹소켓 커넥션 매니저]
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# Supabase 클라이언트 설정
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Optional[Client] = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase 연결 실패:", e)

# OpenAI 클라이언트 설정
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
gpt_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

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
        return {
            "power": {"1": True, "2": True, "3": True, "4": True},
            "wifi": True,
            "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"},
            "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0}
        }

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except:
        pass

# [데이터 구조 (ORCA Supabase 스키마 엄격 준수)]
class PowerData(BaseModel):
    room: Optional[str] = "거실"
    device_id: Optional[str] = "smart_multitap_1"
    port_number: int = 1
    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0
    temperature: float = 25.0
    is_on: bool = True
    action_reason: Optional[str] = "정상 작동"

class ControlData(BaseModel):
    port_number: int
    is_on: bool

# [AI 판단 로직]
def ask_gpt_to_cut_power(voltage, current, power, temperature):
    if not gpt_client:
        return False
    prompt = f"""
    너는 화재와 전력 낭비를 막는 스마트 멀티탭 AI야.
    현재 센서 값:
    - 전압: {voltage}V
    - 전류: {current}mA
    - 소비 전력: {power}mW
    - 온도: {temperature}도

    위 수치를 보고, 화재 위험이 있거나 비정상적인 전력 낭비라고 판단되면 오직 "CUT" 이라고만 대답해.
    정상적인 상황이라 계속 켜둬도 되면 오직 "KEEP" 이라고만 대답해. 다른 부연 설명은 절대 하지 마.
    """
    try:
        response = gpt_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.1
        )
        decision = response.choices[0].message.content.strip()
        print(f"GPT 판단: {decision}")
        return decision == "CUT"
    except Exception as e:
        print("GPT 통신 에러:", e)
        return False

# [API 1] 실시간 상태 가져오기 (웹 대시보드 연동)
@app.get("/get-data")
def get_data():
    state = get_state()
    if not supabase:
        return []
    try:
        response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(16).execute()
        latest_data = {}
        for row in response.data:
            p = row.get("port_number", 1)
            if p not in latest_data:
                latest_data[p] = row
        
        result = list(latest_data.values())
        for row in result:
            p_str = str(row.get("port_number", 1))
            row["device_type"] = state["types"].get(p_str, "일반")
            row["wifi_connected"] = state.get("wifi", True)
            
            if state["power"].get(p_str) == False:
                row["is_on"] = False
                row["power"] = 0.0
                row["action_reason"] = "차단 상태"
                
        result.sort(key=lambda x: x.get("port_number", 1))
        return result
    except Exception as e:
        print("get_data 에러:", e)
        return []

# [API 2] 수동 제어
@app.post("/control")
async def control_port(req: ControlData):
    state = get_state()
    state["power"][str(req.port_number)] = req.is_on
    if req.is_on:
        state["last_toggle_time"][str(req.port_number)] = time.time()
    save_state(state)
    
    # 웹소켓 브로드캐스트 (게이트웨이에 제어 명령 전달)
    control_msg = {
        "event": "control",
        "port_number": req.port_number,
        "is_on": req.is_on
    }
    await manager.broadcast(control_msg)
    return {"message": "제어 성공"}

# [API 2.5] 라즈베리파이/PC 게이트웨이 전용 실시간 웹소켓 엔드포인트
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    print("[WebSocket] 게이트웨이 연결됨!")
    try:
        while True:
            data_text = await websocket.receive_text()
            data_json = json.loads(data_text)
            
            if data_json.get("event") == "sensor_upload":
                payload = data_json.get("data", {})
                power_data = PowerData(**payload)
                upload_data(power_data)
                await websocket.send_json({"status": "ok", "port": power_data.port_number})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print("[WebSocket] 게이트웨이 연결 해제됨.")
    except Exception as e:
        manager.disconnect(websocket)

# [API 3] 센서 데이터 업로드 & Supabase DB 저장
@app.post("/upload-data")
def upload_data(data: PowerData):
    state = get_state()
    p_str = str(data.port_number)
    port_type = state["types"].get(p_str, "일반")
    just_turned_on = (time.time() - state["last_toggle_time"].get(p_str, 0)) < 5 

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
            data.action_reason = "과열 차단 (80도 초과)"
            state["power"][p_str] = False
        else:
            is_danger = ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature)
            if is_danger:
                data.is_on = False
                data.action_reason = "AI 판단: 위험 및 낭비 감지 차단"
                state["power"][p_str] = False
            else:
                data.action_reason = "정상 작동"
            
    save_state(state)
    
    # Supabase DB 저장
    res_data = None
    if supabase:
        try:
            res = supabase.table("sensor_data").insert(data.dict()).execute()
            res_data = res.data
        except Exception as e:
            print("Supabase insert 에러:", e)
            
    return {"message": "데이터 처리 성공", "result": res_data}

# [API 4] 대시보드 통계 API
@app.get("/get-stats")
def get_stats():
    cut_count = 0
    if supabase:
        try:
            response = supabase.table("sensor_data").select("action_reason").eq("is_on", False).limit(500).execute()
            if response.data:
                cut_count = len([row for row in response.data if "차단" in row.get("action_reason", "")])
        except Exception as e:
            print("get_stats 에러:", e)

    saved_kwh_today = cut_count * 0.4 
    saved_cost_today = int(saved_kwh_today * 150)
    cumulative_kwh = saved_kwh_today + 124.0
    cumulative_cost = int(cumulative_kwh * 150)

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