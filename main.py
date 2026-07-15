from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
import json
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

# ⭐️ [핵심 해결책] 다중 일꾼(Worker) 동기화를 위한 공용 장부(파일) 시스템
STATE_FILE = "smart_state.json"

def get_state():
    # 파일이 없으면 초기값 세팅
    if not os.path.exists(STATE_FILE):
        initial = {
            "power": {"1": True, "2": True, "3": True, "4": True},
            "wifi": True,
            "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"}
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
            "types": {"1": "상시", "2": "일반", "3": "일반", "4": "일반"}
        }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# --------------------------------------------------

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

class WifiStatus(BaseModel):
    connected: bool

class PortTypeUpdate(BaseModel):
    port_number: int
    device_type: str 

@app.get("/get-data")
def get_data():
    state = get_state() # 공용 장부 읽기
    
    # 누락 방지를 위해 16개 확보
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
            if not state["wifi"] and state["types"].get(p_str) == "위험":
                row["action_reason"] = "외출 안심 모드: 위험기기 즉시 자동 차단"
            elif row["action_reason"] == "정상 작동" or not row["action_reason"]:
                row["action_reason"] = "차단 유지 중 (앱에서 다시 켜기 대기)"
            
    result.sort(key=lambda x: x["port_number"]) 
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

@app.post("/control")
def control_port(req: ControlData):
    state = get_state()
    state["power"][str(req.port_number)] = req.is_on
    save_state(state) # 장부에 확실히 기록!
    
    state_str = "켜짐" if req.is_on else "꺼짐"
    return {"message": f"포트 {req.port_number} 전원 {state_str} 상태로 수동 변경 완료!"}

@app.post("/set-port-type")
def set_port_type(req: PortTypeUpdate):
    if req.device_type in ["상시", "위험", "일반"]:
        state = get_state()
        p_str = str(req.port_number)
        state["types"][p_str] = req.device_type
        
        if not state["wifi"] and req.device_type == "위험":
            state["power"][p_str] = False
            
        save_state(state)
        return {"message": f"포트 {req.port_number} 기기 타입이 [{req.device_type}기기]로 변경되었습니다."}
    return {"error": "잘못된 기기 타입입니다."}

@app.post("/wifi-status")
def update_wifi_status(req: WifiStatus):
    state = get_state()
    state["wifi"] = req.connected
    
    if not req.connected:
        for port, p_type in state["types"].items():
            if p_type == "위험":
                state["power"][port] = False 
                
    save_state(state)
    status_str = "연결됨" if req.connected else "끊김(외출)"
    return {"message": f"와이파이가 {status_str} 상태로 변경되어 위험기기를 일괄 체크했습니다."}

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
    state = get_state()
    p_str = str(data.port_number)
    
    wifi_connected = state["wifi"]
    port_type = state["types"].get(p_str, "일반")

    if not wifi_connected and port_type == "위험":
        state["power"][p_str] = False

    if state["power"].get(p_str) == False:
        data.is_on = False
        data.voltage = 0.0
        data.current = 0.0
        data.power = 0.0
        if not wifi_connected and port_type == "위험":
            data.action_reason = "외출 안심 모드: 위험기기 즉시 자동 차단"
        else:
            data.action_reason = "차단 유지 중 (앱에서 다시 켜기 대기)"
            
    else:
        if data.temperature >= 80.0:
            data.is_on = False
            data.action_reason = f"포트 {data.port_number} 과열 감지: 즉시 강제 차단"
            state["power"][p_str] = False
        elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
            data.is_on = False
            data.action_reason = f"AI 판단: 포트 {data.port_number} 위험 차단"
            state["power"][p_str] = False
        else:
            data.action_reason = "정상 작동"
    
    save_state(state) # AI나 과열로 차단됐을 수 있으니 최종 장부 덮어쓰기
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공", "result": data}