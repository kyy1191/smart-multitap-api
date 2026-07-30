"""
main.py에 아직 합치기 전, 이 예측 API만 단독으로 띄워서 테스트하기 위한 앱.
    uvicorn ai_power_prediction.standalone_app:app --reload --port 8001
프론트엔드 개발 중에는 API_BASE를 http://localhost:8001 로 맞추면 됨.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router as power_prediction_router

app = FastAPI(title="Smart Multitap - AI Power Prediction (standalone)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(power_prediction_router)
