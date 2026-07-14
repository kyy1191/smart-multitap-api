from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware  # ⭐️ 이거 추가!

app = FastAPI()

# ⭐️ 외부 웹사이트(친구)에서 내 서버에 접속할 수 있게 허락해 주는 방어막 해제 코드!
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 일단 테스트니까 모든 사이트 허용 ("*")
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- (이 아래로는 원래 있던 SUPABASE_URL 등 기존 코드 그대로 유지!) ---
# ⭐️ 여기에 아까 메모장 복사해둔 주소와 키를 따옴표 안에 넣으세요!
SUPABASE_URL = "https://wmkynomraejengmwusyv.supabase.co"
SUPABASE_KEY = "sb_publishable_OdRc1n8jnAsPpvjwZ87x-A_y1rJmEaW"

# 데이터베이스(창고) 관리인 생성!
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ⭐️ 라즈베리파이가 우리에게 보낼 데이터의 '모양'을 규칙으로 정해두기
class PowerData(BaseModel):
    device_id: str
    voltage: float
    current: float
    power: float
    temperature: float
    is_on: bool
    action_reason: str

# 문 1 (POST): 라즈베리파이가 데이터 보낼 때 들어오는 문
@app.post("/upload-data")
def upload_data(data: PowerData):
    # 받은 데이터를 Supabase의 'sensor_data' 테이블에 집어넣기
    # (주의: 만약 아까 만든 테이블 이름이 다르면 sensor_data 부분을 그 이름으로 바꿔야 해!)
    response = supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "DB에 데이터 저장 성공!", "result": response.data}

# 문 2 (GET): 친구(웹 화면)가 데이터 달라고 할 때 나가는 문
@app.get("/get-data")
def get_data():
    # Supabase 창고에서 최신 데이터 10개만 꺼내서 돌려주기
    response = supabase.table("sensor_data").select("*").order("created_at", desc=True).limit(10).execute()
    return response.data