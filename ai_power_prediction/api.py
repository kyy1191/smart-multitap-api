"""
누진세 예측 API 라우터 — main.py와 분리된 독립 모듈 (2026-07-30, main.py 락 상태).

나중에 main.py에 합칠 때는:
    from ai_power_prediction.api import router as power_prediction_router
    app.include_router(power_prediction_router)
그때까지는 standalone_app.py로 단독 실행해서 테스트한다.
"""
from fastapi import APIRouter, HTTPException

from .predict_tax_risk import predict_monthly_tax_risk

router = APIRouter(prefix="/api/power", tags=["power-prediction"])


@router.get("/predict")
def get_power_prediction():
    result = predict_monthly_tax_risk()
    if "error" in result:
        raise HTTPException(status_code=503, detail=result["error"])
    return result
