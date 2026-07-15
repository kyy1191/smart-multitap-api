import requests
import time
import random

URL = "https://smart-multitap-api.onrender.com/upload-data"

print("🚀 가짜 라즈베리파이 가동! (2번 포트의 온도가 실시간으로 오르락내리락 합니다!)")

# ⭐️ 2번 포트의 초기 온도와 변화 방향 설정
p2_temp = 70.0
p2_direction = 1  # 1이면 가열 중(온도 상승), -1이면 냉각 중(온도 하강)

while True:
    try:
        # ⭐️ 2번 포트 온도 변화 로직 (한 바퀴 돌 때마다 온도가 바뀜)
        if p2_direction == 1:
            p2_temp += random.uniform(2.0, 4.0)  # 2~4도씩 팍팍 상승!
            if p2_temp >= 85.0:
                p2_direction = -1  # 85도 찍으면 식기 시작
        else:
            p2_temp -= random.uniform(2.0, 4.0)  # 2~4도씩 쿨링!
            if p2_temp <= 70.0:
                p2_direction = 1   # 70도까지 식으면 다시 가열 시작

        for port in range(1, 5):
            
            if port == 1: # P1: 정상 (TV)
                volt = random.uniform(219.0, 221.0)
                curr = random.uniform(1.0, 1.2)
                temp = random.uniform(28.0, 30.0)
                action = "TV 시청중"
                
            elif port == 2: # P2: 🎢 온도 오르락내리락 (80도 넘으면 서버가 차단함)
                volt = random.uniform(215.0, 220.0)
                curr = random.uniform(10.0, 12.0)
                temp = p2_temp # 위에서 계산한 오르락내리락 온도를 쏙!
                action = "고데기 작동중"
                
            elif port == 3: # P3: 정상 (스마트폰 충전기)
                volt = random.uniform(220.0, 220.5)
                curr = random.uniform(0.05, 0.1)
                temp = random.uniform(24.0, 25.0)
                action = "스마트폰 충전중"
                
            else: # P4: 빈 포트
                volt = 220.0
                curr = 0.0
                temp = 24.0
                action = "대기중"

            data = {
                "room": "거실",
                "device_id": "multitap1",
                "port_number": port,
                "voltage": round(volt, 2),
                "current": round(curr, 2),
                "power": round(volt * curr, 2),
                "temperature": round(temp, 2),
                "is_on": True, # 센서는 무조건 켜져있다고 보내지만, 80도 넘으면 서버가 False로 쳐냄!
                "action_reason": action
            }

            response = requests.post(URL, json=data)
            
            if response.status_code == 200:
                # 콘솔에서 온도 변화를 한눈에 볼 수 있게 출력!
                if port == 2:
                    print(f"🔥 P2 (고데기) 현재 온도: {data['temperature']}도")
                else:
                    print(f"✅ P{port} 전송 성공: {data['power']}W / {data['temperature']}도")
            else:
                print(f"❌ P{port} 전송 실패: {response.status_code}")
                
            time.sleep(1)

        print("-" * 40)
        time.sleep(2) # 조금 더 빠르게 변하는 걸 보려고 대기 시간을 2초로 줄임

    except Exception as e:
        print("에러 발생:", e)
        time.sleep(5)