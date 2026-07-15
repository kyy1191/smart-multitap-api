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

# ⭐️ [신규 1] 와이파이 연결 상태 (기본값: True 연결됨)
wifi_connected = True

# ⭐️ [신규 2] 포트별 기기 설정 분류 ("상시", "위험", "일반")
port_types = {
    1: "상시",  # 1번 포트 기본값: 상시 (TV)
    2: "일반",  # 2번 포트 기본값: 일반 (공기청정기)
    3: "일반",  # 3번 포트 기본값: 일반
    4: "일반"   # 4번 포트 기본값: 일반
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

# 프론트에서 보내주는 와이파이 상태 데이터 구조
class WifiStatus(BaseModel):
    connected: bool

# 프론트에서 보내주는 포트 설정 데이터 구조
class PortTypeUpdate(BaseModel):
    port_number: int
    device_type: str # "상시", "위험", "일반"

@app.get("/get-data")
def get_data():
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(8).execute()
    
    latest_data = {}
    for row in response.data:
        p = row["port_number"]
        if p not in latest_data:
            latest_data[p] = row
            
    result = list(latest_data.values())

    # DB 데이터 보정 및 상태 동기화
    for row in result:
        p = row["port_number"]
        
        # ⭐️ 프론트엔드가 화면을 그리기 편하게 기기 종류와 와이파이 상태를 같이 얹어줌!
        row["device_type"] = port_types.get(p, "일반")
        row["wifi_connected"] = wifi_connected
        
        # 🚨 만약 와이파이가 끊겼는데, 이 기기가 '위험기기'라면? 즉시 메모리 차단!
        if not wifi_connected and port_types.get(p) == "위험":
            port_power_state[p] = False
            
        if port_power_state[p] == False:
            row["is_on"] = False
            row["power"] = 0.0
            if not wifi_connected and port_types.get(p) == "위험":
                row["action_reason"] = "외출 안심 모드: 위험기기 즉시 자동 차단"
            elif row["action_reason"] == "정상 작동" or not row["action_reason"]:
                row["action_reason"] = "차단 유지 중 (앱에서 다시 켜기 대기)"
            
    result.sort(key=lambda x: x["port_number"]) # 포트 순서대로 이쁘게 정렬
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
    port_power_state[req.port_number] = req.is_on
    state_str = "켜짐" if req.is_on else "꺼짐"
    return {"message": f"포트 {req.port_number} 전원 {state_str} 상태로 수동 변경 완료!"}

# ⭐️ [신규 API 1] 앱에서 기기 종류 드롭다운을 변경할 때 사용!
@app.post("/set-port-type")
def set_port_type(req: PortTypeUpdate):
    if req.device_type in ["상시", "위험", "일반"]:
        port_types[req.port_number] = req.device_type
        # 이미 와이파이가 끊긴 외출 상태에서 이 포트를 '위험'으로 변경하면 즉시 차단하는 센스!
        if not wifi_connected and req.device_type == "위험":
            port_power_state[req.port_number] = False
        return {"message": f"포트 {req.port_number} 기기 타입이 [{req.device_type}기기]로 변경되었습니다."}
    return {"error": "잘못된 기기 타입입니다. (상시, 위험, 일반만 가능)"}

# ⭐️ [신규 API 2] 앱에서 와이파이 시뮬레이션 버튼 누를 때 사용!
@app.post("/wifi-status")
def update_wifi_status(req: WifiStatus):
    global wifi_connected
    wifi_connected = req.connected
    
    # 외출(와이파이 차단) 시 위험기기는 즉시 전원 OFF로 잠금 처리
    if not wifi_connected:
        for port, p_type in port_types.items():
            if p_type == "위험":
                port_power_state[port] = False # 완전 차단 락(Lock)
                
    status_str = "연결됨" if wifi_connected else "끊김(외출)"
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

# 센서에서 데이터 들어올 때
@app.post("/upload-data")
def upload_data(data: PowerData):
    # 와이파이 끊김 + 위험기기 상황이면 센서 수치 올릴 필요도 없이 즉시 차단
    if not wifi_connected and port_types.get(data.port_number) == "위험":
        port_power_state[data.port_number] = False

    # 꺼진 상태라면 데이터 강제 무시
    if port_power_state[data.port_number] == False:
        data.is_on = False
        data.voltage = 0.0
        data.current = 0.0
        data.power = 0.0
        if not wifi_connected and port_types.get(data.port_number) == "위험":
            data.action_reason = "외출 안심 모드: 위험기기 즉시 자동 차단"
        else:
            data.action_reason = "차단 유지 중 (앱에서 다시 켜기 대기)"
        
    else:
        if data.temperature >= 80.0:
            data.is_on = False
            data.action_reason = f"포트 {data.port_number} 과열 감지: 즉시 강제 차단"
            port_power_state[data.port_number] = False
        elif ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature):
            data.is_on = False
            data.action_reason = f"AI 판단: 포트 {data.port_number} 위험 차단"
            port_power_state[data.port_number] = False
        else:
            data.action_reason = "정상 작동"
    
    supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공", "result": data}