from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
import time
import asyncio
from datetime import datetime, timezone
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

# [AI 누진세 예측 기능] 2026-07-30 병합 - ai_power_prediction/ 은 완전히 분리된 모듈이라
# main.py의 나머지 로직과 아무 상태도 공유하지 않음, 실패해도(패키지 누락 등) 서버 전체가
# 죽지 않게 try/except로 감쌈.
try:
    from ai_power_prediction.api import router as power_prediction_router
    app.include_router(power_prediction_router)
except Exception as e:
    print("[main] AI 누진세 예측 라우터 로드 실패 (기능만 비활성화, 서버는 계속 실행):", e)

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
            "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0},
            "db_upload_enabled": True
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
            "last_toggle_time": {"1": 0, "2": 0, "3": 0, "4": 0},
            "db_upload_enabled": True
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

# [API 2.1] 포트 타입 설정/조회 (상시/일반/위험 - 프론트엔드에만 있고 서버엔 없던 걸 저장하도록 추가)
# 프론트엔드(index.html)가 이미 POST /set-port { port_number, device_type }로 호출하고 있었음
# (백엔드에 대응 엔드포인트가 없어 조용히 실패 중이었음 - 필드명/경로를 프론트에 맞춤)
class PortTypeUpdate(BaseModel):
    port_number: int
    device_type: str  # "상시" | "일반" | "위험"

@app.get("/port-types")
def get_port_types():
    return get_state().get("types", {})

@app.post("/set-port")
def set_port_type(req: PortTypeUpdate):
    state = get_state()
    state["types"][str(req.port_number)] = req.device_type
    save_state(state)
    return {"message": "타입 저장 완료", "port_number": req.port_number, "device_type": req.device_type}

# [전력 지문(Fingerprint) 인식 기능] 2026-07-31
# 별도 Next.js 프로토타입(AI_Fingerprint_Platform)에서 검증한 로직(utils/similarity.ts,
# lib/trainFromHistory.ts)을 그대로 Python으로 이식. Supabase `fingerprints` 테이블에
# 학습 통계를 영구 저장(서버 재시작/재배포에도 유지) - 예전엔 JS 배열 메모리에만 있어서
# 매번 날아갔음. 대시보드 포트 카드에 AI 인식 결과를 얹기 위해 /get-data와 sensor_upload
# 브로드캐스트 둘 다에 fingerprint_matches/recognized_device_name을 채워 넣는다.
FP_CONFIDENCE_THRESHOLD = 55.0  # 이 이상이어야 "인식됨"으로 취급
FP_CONFIDENCE_GAP = 15.0        # 2위와 이만큼 이상 벌어져야 확정 (애매하면 PORT#로 표시)
# 2026-07-31: 기존 65/20이 LED 예열에 따른 자연스러운 전력 드리프트(±20~25%)에도
# 너무 쉽게 "판별 대기"로 빠져서 55/15로 완화 - 동시에 fingerprints의 std_power/std_current도
# 실측 대비 최소 12%로 하한선을 둬서(위 학습값들) 드리프트에 더 관대해지도록 함께 조정함.
FP_CACHE_TTL = 30               # 초 - 매 센서 메시지마다 DB 조회하지 않도록 캐싱

fingerprints_cache: List[dict] = []
fingerprints_cache_time = 0.0

def load_fingerprints_from_db() -> List[dict]:
    if not supabase:
        return []
    try:
        res = supabase.table("fingerprints").select("*").order("device_name").execute()
        return res.data or []
    except Exception as e:
        print("[fingerprints] 조회 에러:", e)
        return []

def get_fingerprints_cached() -> List[dict]:
    global fingerprints_cache, fingerprints_cache_time
    now = time.time()
    if now - fingerprints_cache_time > FP_CACHE_TTL:
        fingerprints_cache = load_fingerprints_from_db()
        fingerprints_cache_time = now
    return fingerprints_cache

def match_device_to_fingerprints(power: float, current: float, voltage: float, fingerprints: List[dict]) -> List[dict]:
    if not fingerprints:
        return []
    scored = []
    for fp in fingerprints:
        d_power = (power - fp.get("avg_power", 0)) / max(fp.get("std_power", 0) or 0, 0.5)
        d_current = (current - fp.get("avg_current", 0)) / max(fp.get("std_current", 0) or 0, 0.01)
        d_voltage = (voltage - fp.get("avg_voltage", 5.2)) / 5
        dist = (d_power ** 2 + d_current ** 2 + d_voltage ** 2 * 0.2) ** 0.5
        scored.append({"device_name": fp["device_name"], "score": 1 / (1 + dist)})
    total = sum(s["score"] for s in scored) or 1
    ranked = [{"device_name": s["device_name"], "confidence": round((s["score"] / total) * 100, 1)} for s in scored]
    ranked.sort(key=lambda x: x["confidence"], reverse=True)
    return ranked

def recognized_name_from_matches(matches: List[dict]) -> Optional[str]:
    if not matches:
        return None
    top = matches[0]
    if top["confidence"] < FP_CONFIDENCE_THRESHOLD:
        return None
    if len(matches) > 1 and (top["confidence"] - matches[1]["confidence"]) < FP_CONFIDENCE_GAP:
        return None
    return top["device_name"]

def attach_fingerprint_fields(row: dict) -> dict:
    fingerprints = get_fingerprints_cached()
    matches = match_device_to_fingerprints(row.get("power", 0.0), row.get("current", 0.0), row.get("voltage", 0.0), fingerprints)
    row["fingerprint_matches"] = matches
    row["recognized_device_name"] = recognized_name_from_matches(matches)
    row["recognition_confidence"] = matches[0]["confidence"] if matches else None
    return row

class FingerprintTrainRequest(BaseModel):
    device_name: str
    port_number: int

@app.get("/fingerprints")
def get_fingerprints():
    return {"fingerprints": get_fingerprints_cached()}

@app.post("/fingerprints/train")
def train_fingerprint(req: FingerprintTrainRequest):
    if not supabase:
        return {"status": "error", "message": "Supabase 연동이 끊겨 있습니다."}

    # PostgREST 기본 max-rows=1000이라 .range()로 페이지네이션해서 이 포트가 켜져있던
    # 동안의(is_on=true) 실측 히스토리 전체를 긁어옴 - 꺼짐 상태를 섞으면 표준편차가
    # 비정상적으로 커져서 다른 포트 데이터까지 이 지문 쪽으로 오분류되는 문제가 있었음.
    PAGE_SIZE = 1000
    MAX_SAMPLES = 20000
    rows: List[dict] = []
    offset = 0
    while len(rows) < MAX_SAMPLES:
        try:
            res = (
                supabase.table("sensor_data")
                .select("power,current,voltage")
                .eq("device_id", "smart_multitap_1")
                .eq("port_number", req.port_number)
                .eq("is_on", True)
                .range(offset, offset + PAGE_SIZE - 1)
                .execute()
            )
        except Exception as e:
            return {"status": "error", "message": f"sensor_data 조회 실패: {e}"}
        page = res.data or []
        if not page:
            break
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    if not rows:
        return {"status": "error", "message": "학습할 실측 데이터가 없습니다 (해당 포트가 켜진 기록 없음)."}

    powers = [float(r.get("power") or 0) for r in rows]
    currents = [float(r.get("current") or 0) for r in rows]
    voltages = [float(r.get("voltage") or 0) for r in rows]

    avg_power = sum(powers) / len(powers)
    avg_current = sum(currents) / len(currents)
    avg_voltage = sum(voltages) / len(voltages)
    std_power = (sum((p - avg_power) ** 2 for p in powers) / max(len(powers) - 1, 1)) ** 0.5
    std_current = (sum((c - avg_current) ** 2 for c in currents) / max(len(currents) - 1, 1)) ** 0.5

    distances = []
    for p, c in zip(powers, currents):
        dp = (p - avg_power) / max(std_power, 0.5)
        dc = (c - avg_current) / max(std_current, 0.05)
        distances.append((dp ** 2 + dc ** 2) ** 0.5)
    avg_similarity = sum(100 / (1 + d) for d in distances) / len(distances)

    fp_row = {
        "device_name": req.device_name,
        "port_number": req.port_number,
        "avg_power": round(avg_power, 2),
        "avg_current": round(avg_current, 3),
        "avg_voltage": round(avg_voltage, 3),
        "std_power": round(std_power, 2),
        "std_current": round(std_current, 3),
        "status": "trained",
        "training_count": len(rows),
        "average_similarity": round(avg_similarity, 1),
        "last_trained_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("fingerprints").upsert(fp_row, on_conflict="device_name").execute()
    except Exception as e:
        return {"status": "error", "message": f"fingerprints 저장 실패: {e}"}

    global fingerprints_cache_time
    fingerprints_cache_time = 0.0  # 다음 조회 때 즉시 새로 불러오도록 캐시 무효화

    return {"status": "success", "fingerprint": fp_row}

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

        attach_fingerprint_fields(row)

    result.sort(key=lambda x: x.get("port_number", 1))
    return result

# [API 2] 수동 제어
@app.post("/control")
async def control_port(req: ControlData):
    state = get_state()
    state["power"][str(req.port_number)] = req.is_on
    if req.is_on:
        state["last_toggle_time"][str(req.port_number)] = time.time()
        # 대시보드에서 다시 켜면 과열/AI 안전차단 잠금도 함께 해제 (예전부터 가능했던 동작, 유지)
        state.setdefault("safety_lock", {})[str(req.port_number)] = False
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

# [API 2.3] DB 전송 ON/OFF 제어 (로컬 컨트롤 패널용 - 테스트 중 Supabase에 데이터 안 쌓게)
class DbUploadToggle(BaseModel):
    enabled: bool

@app.get("/db-upload-status")
def get_db_upload_status():
    return {"enabled": get_state().get("db_upload_enabled", True)}

@app.post("/toggle-db-upload")
def toggle_db_upload(req: DbUploadToggle):
    state = get_state()
    state["db_upload_enabled"] = req.enabled
    save_state(state)
    return {"message": "설정 완료", "enabled": req.enabled}

# [API 2.4] 사용자(대시보드) Wi-Fi 연결 상태 기록 - 위험기기 외출 자동차단 로직이 호출
class WifiStatus(BaseModel):
    connected: bool
    ssid: str = ""

@app.get("/wifi-status")
def get_wifi_status():
    state = get_state()
    return {
        "connected": state.get("wifi", True),
        "ssid": state.get("wifi_ssid", ""),
        "updated_at": state.get("wifi_updated_at")
    }

@app.post("/wifi-status")
def update_wifi_status(req: WifiStatus):
    state = get_state()
    state["wifi"] = req.connected
    state["wifi_ssid"] = req.ssid
    state["wifi_updated_at"] = time.time()
    save_state(state)
    return {"message": "기록 완료", "connected": req.connected}

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
    # process_sensor_data가 data 객체를 그 자리에서 직접 수정하므로,
    # 비교 기준으로 쓸 "ESP32가 원래 보낸 값"은 호출 전에 미리 따로 저장해둬야 함
    # (안 그러면 아래 비교가 항상 같은 값끼리 비교하게 돼서 死코드가 됨 - 2026-07-30 발견/수정)
    original_is_on = data.is_on

    loop = asyncio.get_running_loop()
    res_dict = await loop.run_in_executor(None, process_sensor_data, data)

    # 서버에 저장된(혹은 방금 결정된) 최종 릴레이 상태
    final_is_on = get_state()["power"].get(str(data.port_number), False)

    # AI 판단으로 차단되었거나, 상시기기인데 꺼져있어서 켜야 하거나 등
    # 게이트웨이(기기)로 실제 제어 명령을 내려야 하는 경우 동기화 패킷 전송
    if res_dict.get("is_on") != original_is_on:
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
#
# 2026-07-30 재설계: 예전엔 state["power"][p]==False면 무조건 최우선으로 차단 상태를 유지했는데,
# 이 플래그가 "대시보드 수동 OFF"든 "AI/과열 안전차단"이든 구분 없이 똑같이 취급되면서
# 물리 스위치를 아무리 켜도 서버가 계속 예전 수동OFF 기록으로 되돌려버리는 문제가 있었음
# (제어 동기화 버그를 고치고 나서야 이게 실제로 기기까지 전달되기 시작하며 드러남).
# 이제는 안전차단(과열/AI)만 state["safety_lock"]로 따로 표시해서 물리 스위치도 못 이기게 하고,
# 그 외에는 기기가 실제로 보고하는 릴레이 상태(=물리 스위치 조작 결과 포함)를 그대로 신뢰한다.
def process_sensor_data(data: PowerData):
    global db_buffer, last_flush_time
    state = get_state()
    p_str = str(data.port_number)
    port_type = state["types"].get(p_str, "일반")

    if state.get("safety_lock", {}).get(p_str):
        # 과열/AI 위험판단으로 이미 강제 차단된 상태 - 물리 스위치로도 못 풀리고, 대시보드에서 다시 켜야만 해제됨
        data.is_on = False
        data.action_reason = state.get("safety_lock_reason", {}).get(p_str, "안전 차단 상태")
        state["power"][p_str] = False
    elif data.temperature >= 80.0:
        data.is_on = False
        data.action_reason = "과열 차단 (80도 초과)"
        state["power"][p_str] = False
        state.setdefault("safety_lock", {})[p_str] = True
        state.setdefault("safety_lock_reason", {})[p_str] = data.action_reason
    else:
        # 물리 스위치가 이김: 기기가 방금 보고한 on/off를 그대로 서버 상태에 반영
        state["power"][p_str] = data.is_on
        if not data.is_on:
            data.action_reason = "차단 상태"
        elif port_type == "상시":
            data.action_reason = "상시기기 (항시 작동)"
        else:
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
                        state.setdefault("safety_lock", {})[p_str] = True
                        state.setdefault("safety_lock_reason", {})[p_str] = data.action_reason

    save_state(state)

    data_dict = data.dict()

    # 📦 1. Supabase DB 저장을 위해 버퍼에 누적 (로컬 컨트롤 패널에서 꺼놨으면 통째로 건너뜀 - 테스트 중 DB에 안 쌓이게)
    # ⚠️ sensor_data 테이블 스키마에 없는 컬럼(fingerprint_matches 등)이 섞이면 insert가 실패하므로
    # 얕은 복사본을 넣는다 - 아래에서 data_dict에 AI 인식 필드를 추가로 얹기 전에 분리.
    if state.get("db_upload_enabled", True):
        db_buffer.append(dict(data_dict))

        if len(db_buffer) >= MAX_BUFFER_SIZE or (time.time() - last_flush_time) >= FLUSH_INTERVAL:
            flush_db_buffer()

    # ⚡ 2. 실시간 캐시/브로드캐스트용에는 AI 인식 결과(fingerprint_matches 등)까지 얹어서 반환
    attach_fingerprint_fields(data_dict)
    latest_live_cache[data.port_number] = data_dict

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