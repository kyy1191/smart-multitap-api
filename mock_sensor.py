import requests
import time
import random

# 네 Render 서버 주소! (뒤에 /upload-data 붙임)
URL = "https://smart-multitap-api.onrender.com/upload-data"

print("🚀 가짜 라즈베리파이 센서 가동 시작! (Ctrl+C를 누르면 멈춥니다)")

while True:
    try:
        # 4개의 포트 데이터를 한 번씩 쏨
        for port in range(1, 5):
            
            # 각 포트별 컨셉 설정 (UI에서 그래프가 예쁘게 변하도록!)
            if port == 1: # P1: 상시기기 (거실 TV 컨셉 - 전력 변화 적음)
                volt = random.uniform(219.0, 221.0)
                curr = random.uniform(1.0, 1.2)
                temp = random.uniform(28.0, 30.0)
                action = "TV 시청중"
                
            elif port == 2: # P2: 위험기기 (고데기 컨셉 - 온도 계속 올라감)
                volt = random.uniform(215.0, 220.0)
                curr = random.uniform(5.0, 6.5)
                temp = random.uniform(50.0, 75.0) # 온도가 높음!
                action = "고데기 사용중"
                
            elif port == 3: # P3: 충전기 (대기전력 컨셉 - 매우 낮음)
                volt = random.uniform(220.0, 220.5)
                curr = random.uniform(0.05, 0.1)
                temp = random.uniform(24.0, 25.0)
                action = "스마트폰 충전중"
                
            else: # P4: 빈 포트
                volt = 220.0
                curr = 0.0
                temp = 24.0
                action = "대기중"

            # 보낼 데이터 포장
            data = {
                "room": "거실",
                "device_id": "multitap1",  # ⭐️ 여기를 'multitap1'으로 고정!
                "port_number": port,       # ⭐️ 포트 번호로만 구분!
                "voltage": round(volt, 2),
                "current": round(curr, 2),
                "power": round(volt * curr, 2),
                "temperature": round(temp, 2),
                "is_on": True,
                "action_reason": action
            }

            # 서버로 데이터 발사!
            response = requests.post(URL, json=data)
            
            if response.status_code == 200:
                print(f"✅ P{port} 전송 성공: {data['power']}W / {data['temperature']}도")
            else:
                print(f"❌ P{port} 전송 실패: {response.status_code}")
                
            # 1초 대기 후 다음 포트 발사
            time.sleep(1)

        print("-" * 40)
        # 4개 다 쏘고 3초 쉬었다가 다시 반복!
        time.sleep(3)

    except Exception as e:
        print("에러 발생! 서버가 켜져 있는지 확인해봐:", e)
        time.sleep(5)