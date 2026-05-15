import requests

TOKEN = "8899714643:AAGzrPLNqwGvg9WUQ7--n5OBvoMvlO7J94I"
url = f"https://api.telegram.org/bot{TOKEN}/getMe"

try:
    response = requests.get(url, timeout=10)
    print("Статус:", response.status_code)
    print("Ответ:", response.json())
except Exception as e:
    print("Ошибка:", e)