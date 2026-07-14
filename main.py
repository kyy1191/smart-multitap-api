from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from fastapi.middleware.cors import CORSMiddleware
import os
from openai import OpenAI  # ⭐️ GPT 도구 불러오기!

app = FastAPI()

# (CORS 설정 및 Supabase 설정은 원래 있던 대로 유지)

# ⭐️ 1. 메모장에 적어둔 GPT API 키를 여기에 넣기
# 기존에 진짜 키가 적혀있던 따옴표를 지우고 아래처럼 수정!
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") 
gpt_client = OpenAI(api_key=OPENAI_API_KEY)

# ⭐️ 2. GPT가 상황을 판단하는 함수 만들기
def ask_gpt_to_cut_power(voltage, current, power, temperature):
    # GPT에게 상황을 설명하고 지시를 내리는 '프롬프트'
    prompt = f"""
    너는 화재와 전력 낭비를 막는 스마트 멀티탭 AI야.
    현재 센서 값:
    - 전압: {voltage}V
    - 전류: {current}A
    - 소비 전력: {power}W
    - 온도: {temperature}도

    위 수치를 보고, 화재 위험이 있거나 비정상적인 전력 낭비라고 판단되면 오직 "CUT" 이라고만 대답해.
    정상적인 상황이라 계속 켜둬도 되면 오직 "KEEP" 이라고만 대답해. 다른 부연 설명은 절대 하지 마.
    """

    try:
        # GPT에게 질문 쏘기 (gpt-4o-mini 모델이 제일 빠르고 가성비가 좋아!)
        response = gpt_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.1
        )
        
        # GPT의 대답 꺼내기
        decision = response.choices[0].message.content.strip()
        print(f"GPT의 판단: {decision}")
        
        if decision == "CUT":
            return True  # 전원 차단해라!
        else:
            return False # 차단하지 마라!
            
    except Exception as e:
        print("GPT 통신 에러:", e)
        return False # 에러 나면 일단 끄지 말고 대기

# ⭐️ 3. 데이터가 들어올 때 GPT에게 먼저 물어보기
@app.post("/upload-data")
def upload_data(data: PowerData):
    
    # [일반 안전 로직] 온도가 80도를 넘으면 GPT 물어볼 것도 없이 무조건 차단!
    if data.temperature >= 80.0:
        data.is_on = False
        data.action_reason = "과열 감지: 즉시 강제 차단"
        
    # [AI 판단 로직] 그 외의 상황은 GPT에게 물어봐서 판단!
    else:
        is_danger = ask_gpt_to_cut_power(data.voltage, data.current, data.power, data.temperature)
        if is_danger:
            data.is_on = False
            data.action_reason = "AI 판단: 위험 및 낭비 감지 차단"
        else:
            data.action_reason = "정상 작동"

    # 최종 결과 DB에 저장
    response = supabase.table("sensor_data").insert(data.dict()).execute()
    return {"message": "데이터 처리 성공", "result": response.data}