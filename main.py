from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import time
import asyncio
from typing import List, Optional
from google import genai
from google.genai import types

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
# SUPABASE_SERVICE_KEY(secret key, RLS 우회)가 있으면 그걸 우선 사용하고,
# 없으면 기존 SUPABASE_KEY(publishable key)로 폴백 (하위 호환)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
supabase: Optional[Client] = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase 연결 실패:", e)

# Gemini 클라이언트 설정
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gpt_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_MODEL = "gemini-flash-latest"

STATE_FILE = "/tmp/smart_state.json"

# [실시간 메모리 캐시 & Supabase 배치 버퍼 레지스터]
latest_live_cache = {}  # { port_number: dict } -> 웹 대시보드 즉시 반환용 (실시간)
db_buffer = []          # [ dict, ... ] -> Supabase 모아서 분할 저장용 (버퍼링)
last_flush_time = time.time()
FLUSH_INTERVAL = 10      # 10초 주기 DB 저장
MAX_BUFFER_SIZE = 10     # 10개 누적 시 저장

# [장부 시스템] 전역 상태 변수로 관리하여 I/O 경합 완전 차단
global_state = None

def get_state():
    global global_state
    if global_state is not None:
        return global_state

    if not os.path.exists(STATE_FILE):
        initial = {
            "power": {"1": True, "2": True, "3": True, "4": True},
            "wifi": True,
            "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"},
            "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0}
        }
        global_state = initial
        save_state(initial)
        return initial
    try:
        with open(STATE_FILE, "r") as f:
            global_state = json.load(f)
            return global_state
    except:
        global_state = {
            "power": {"1": True, "2": True, "3": True, "4": True},
            "wifi": True,
            "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"},
            "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0}
        }
        return global_state

def save_state(state):
    global global_state
    global_state = state
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

    위 수치를 보고 화재 위험이 있거나, 비정상적인 낭비, 또는 기기를 사용하지 않는 미세전력/대기전력 상태(예: 50mW 미만)라고 판단되면 즉시 전력을 차단하기 위해 "CUT"으로 판단해.
    기기가 정상적으로 활발히 사용중이어서 계속 켜둬도 되면 "KEEP"으로 판단해.
    """
    try:
        response = gpt_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=500,
                response_mime_type="application/json",
                response_schema=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"decision": types.Schema(type=types.Type.STRING, enum=["CUT", "KEEP"])},
                    required=["decision"],
                ),
            ),
        )
        decision = json.loads(response.text)["decision"]
        print(f"AI 판단: {decision}")
        return decision == "CUT"
    except Exception as e:
        print("Gemini 통신 에러:", e)
        return False

# [Supabase DB 배치 플러시 함수]
def flush_db_buffer():
    global db_buffer, last_flush_time
    if not db_buffer or not supabase:
        return
    try:
        data_to_insert = list(db_buffer)
        db_buffer.clear()
        last_flush_time = time.time()
        supabase.table("sensor_data").insert(data_to_insert).execute()
        print(f"[Supabase 배치 저장 완료] {len(data_to_insert)}개 데이터 저장됨.")
    except Exception as e:
        print("[Supabase 배치 저장 에러]:", e)

# [API 1] 실시간 상태 가져오기 (웹 대시보드 연동: 메모리 캐시 + DB 최신 조합)
@app.get("/get-data")
def get_data():
    state = get_state()
    latest_data = {}

    # 1. DB에서 기존 최신 로그 가져오기
    if supabase:
        try:
            response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(30).execute()
            if response.data:
                for row in response.data:
                    p = row.get("port_number", 1)
                    if p not in latest_data:
                        latest_data[p] = row
        except Exception as e:
            print("DB get-data 에러:", e)

    # 2. 실시간 메모리 캐시 데이터가 있으면 최신 실시간 수치로 덮어쓰기 (즉시 표출)
    for p, live_row in latest_live_cache.items():
        latest_data[p] = dict(live_row)

    # 3. 포트 1이 캐시에도 DB에도 없을 때만(콜드 스타트 등) 안전장치로 DB에서 강제 조회.
    #    캐시가 있으면 그게 항상 더 최신이므로(DB는 최대 FLUSH_INTERVAL초 배치 저장) 덮어쓰지 않음.
    if supabase and 1 not in latest_data:
        try:
            p1_res = supabase.table("sensor_data").select("*").eq("device_id", "smart_multitap_1").eq("port_number", 1).order("created_at", desc=True).limit(1).execute()
            if p1_res.data:
                latest_data[1] = p1_res.data[0]
        except Exception as e:
            print("포트 1 DB 쿼리 에러:", e)

    # 기본값 보장 (포트 1 ~ 4)
    for p_num in range(1, 5):
        if p_num not in latest_data:
            latest_data[p_num] = {
                "device_id": "smart_multitap_1",
                "room": "거실",
                "port_number": p_num,
                "voltage": 0.0,
                "current": 0.0,
                "power": 0.0,
                "temperature": 25.0,
                "is_on": state["power"].get(str(p_num), True),
                "action_reason": "정상 작동"
            }

    result = list(latest_data.values())
    for row in result:
        p_str = str(row.get("port_number", 1))
        row["device_type"] = state["types"].get(p_str, "일반")
        row["wifi_connected"] = state.get("wifi", True)
        
        if state["power"].get(p_str) == False:
            row["is_on"] = False
            row["action_reason"] = "차단 상태"

    result.sort(key=lambda x: x.get("port_number", 1))
    return result

# [API 2] 수동 제어
@app.post("/control")
async def control_port(req: ControlData):
    state = get_state()
    state["power"][str(req.port_number)] = req.is_on
    if req.is_on:
        state["last_toggle_time"][str(req.port_number)] = time.time()
    save_state(state)

    # 실시간 캐시 업데이트
    if req.port_number in latest_live_cache:
        latest_live_cache[req.port_number]["is_on"] = req.is_on

    # 게이트웨이에 즉각 제어 명령 전달 (웹소켓)
    await manager.broadcast({
        "event": "control",
        "port_number": req.port_number,
        "is_on": req.is_on
    })

    return {"message": "제어 성공"}

# [API 2.2] Supabase DB 센서 데이터 전체 초기화
@app.post("/clear-db")
def clear_db():
    if not supabase:
        return {"status": "error", "message": "Supabase 연동이 끊겨 있습니다."}
    try:
        # Timestamp 타입에 호환되는 올바른 쿼리로 전체 데이터 삭제
        res = supabase.table("sensor_data").delete().gte("created_at", "1970-01-01T00:00:00Z").execute()
        return {"status": "success", "message": "DB 센서 데이터 전체 초기화 성공 (모두 삭제됨)"}
    except Exception as e:
        print("DB 초기화 에러:", e)
        return {"status": "error", "message": str(e)}

# [API 2.5] 라즈베리파이/PC 게이트웨이 전용 실시간 웹소켓 엔드포인트
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    print("[WebSocket] 새로운 클라이언트 연결됨!")
    try:
        while True:
            data_text = await websocket.receive_text()
            data_json = json.loads(data_text)

            if data_json.get("event") == "sensor_upload":
                payload = data_json.get("data", {})
                
                # 백그라운드 태스크에서 센서 데이터를 정제/판단한 후
                # 최종 데이터를 프론트엔드로 브로드캐스트합니다.
                asyncio.create_task(async_process_sensor_data(PowerData(**payload)))
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        print("[WebSocket] 연결 해제됨.")
    except Exception as e:
        manager.disconnect(websocket)

# [비동기 센서 데이터 처리 백그라운드 태스크]
async def async_process_sensor_data(data: PowerData):
    loop = asyncio.get_running_loop()
    res_dict = await loop.run_in_executor(None, process_sensor_data, data)
    
    # 서버에 저장된(혹은 방금 결정된) 최종 릴레이 상태
    final_is_on = get_state()["power"].get(str(data.port_number), False)
    
    # AI 판단으로 차단되었거나, 상시기기인데 꺼져있어서 켜야 하거나 등
    # 게이트웨이(기기)로 실제 제어 명령을 내려야 하는 경우 동기화 패킷 전송
    if res_dict.get("is_on") != data.is_on: 
        await manager.broadcast({
            "event": "control",
            "port_number": data.port_number,
            "is_on": final_is_on
        })

    # 프론트엔드 대시보드로는 서버가 정제하고 결정한 최종 데이터를 보냄 (상태 꼬임 방지)
    await manager.broadcast({
        "event": "sensor_upload",
        "data": res_dict
    })

# [API 3] 센서 데이터 수신 처리 (실시간 표출 + DB 버퍼링)
def process_sensor_data(data: PowerData):
    global db_buffer, last_flush_time
    state = get_state()
    p_str = str(data.port_number)
    port_type = state["types"].get(p_str, "일반")
    if state["power"].get(p_str) == False:
        data.is_on = False
        data.action_reason = "차단 상태"
    elif port_type == "상시":
        data.is_on = True
        data.action_reason = "상시기기 (항시 작동)"
    else:
        if data.temperature >= 80.0:
            data.is_on = False
            data.action_reason = "과열 차단 (80도 초과)"
            state["power"][p_str] = False
        else:
            # 실시간 전력 표출 딜레이를 최소화하기 위해 GPT 판단 빈도를 조절하거나,
            # 현재 릴레이 On 상태면 우선 무조건 정상으로 표시
            data.action_reason = "정상 작동"
            
            # (GPT 판단은 딜레이가 심하므로, 50W 이상이거나 온도가 60도 이상일 때만 10초에 한 번 선별적 호출로 최적화)
            # 실제 센서 단위는 mW이므로 50W = 50000mW로 비교해야 함 (mW 그대로 비교하면 거의 모든 부하에서 AI가 호출되어 사소한 부하도 오탐 차단됨)
            if data.power > 50000.0 or data.temperature > 60.0:
                last_gpt = state.get("last_gpt_time", {}).get(p_str, 0)
                if time.time() - last_gpt > 10:
                    state.setdefault("last_gpt_time", {})[p_str] = time.time()
                    is_danger = ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature)
                    if is_danger:
                        data.is_on = False
                        data.action_reason = "AI 판단: 위험 및 낭비 감지 차단"
                        state["power"][p_str] = False
            
    save_state(state)

    data_dict = data.dict()
    # ⚡ 1. 실시간 표출용 메모리 캐시에 즉시 업데이트 (대시보드는 항상 최신 실시간 수치 표시)
    latest_live_cache[data.port_number] = data_dict

    # 📦 2. Supabase DB 저장을 위해 버퍼에 누적
    db_buffer.append(data_dict)

    # 3. 버퍼 개수가 모였거나 일정 시간이 지나면 DB에 모아서 저장 (배치 인서트)
    if len(db_buffer) >= MAX_BUFFER_SIZE or (time.time() - last_flush_time) >= FLUSH_INTERVAL:
        flush_db_buffer()

    return data_dict

@app.post("/upload-data")
def upload_data(data: PowerData):
    res_dict = process_sensor_data(data)
    return {"message": "데이터 처리 성공", "result": res_dict}

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