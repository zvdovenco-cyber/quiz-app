import uvicorn
import json
import sqlite3
import uuid
import os
from fastapi import FastAPI, Request, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from openai import OpenAI

app = FastAPI()
templates = Jinja2Templates(directory="templates")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tests.db")

# Сервер будет получать API ключ из переменных окружения Render
GROQ_KEY = os.getenv("GROQ_API_KEY", "")

client = OpenAI(
    api_key=GROQ_KEY,
    base_url="https://api.groq.com/openai/v1"
)

# Инициализация БД
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tests (
            id TEXT PRIMARY KEY,
            questions_json TEXT NOT NULL
        )
    """)
    
    cursor.execute("PRAGMA table_info(tests)")
    columns = [column[1] for column in cursor.fetchall()]
    if "teacher_username" not in columns:
        cursor.execute("ALTER TABLE tests ADD COLUMN teacher_username TEXT DEFAULT 'admin'")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id TEXT NOT NULL,
            student_name TEXT NOT NULL,
            score INTEGER NOT NULL,
            correct_count INTEGER NOT NULL,
            total_questions INTEGER NOT NULL
        )
    """)
    
    conn.commit()
    conn.close()

init_db()

def get_current_user(request: Request):
    username = request.cookies.get("user")
    if not username:
        return None
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM teachers WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return None
    return username

@app.get("/reset-session")
async def reset_session():
    response = RedirectResponse(url="/register", status_code=303)
    response.delete_cookie("user")
    return response

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")

@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT password FROM teachers WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0] == password:
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="user", value=username)
        return response

    return templates.TemplateResponse(request=request, name="login.html", context={"error": "Неверный логин или пароль"})

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html")

@app.post("/register", response_class=HTMLResponse)
async def register(request: Request, username: str = Form(...), password: str = Form(...)):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO teachers (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        conn.close()
        
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(key="user", value=username)
        return response
    except sqlite3.IntegrityError:
        return templates.TemplateResponse(request=request, name="register.html", context={"error": "Пользователь с таким логином уже существует"})

@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login")
    response.delete_cookie("user")
    return response

@app.get("/", response_class=HTMLResponse)
async def teacher_page(request: Request):
    user = get_current_user(request)
    if not user:
        response = RedirectResponse(url="/login")
        response.delete_cookie("user")
        return response

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, questions_json FROM tests WHERE teacher_username = ?", (user,))
    rows = cursor.fetchall()
    conn.close()

    my_tests = [
        {"id": r[0], "questions_count": len(json.loads(r[1]))}
        for r in rows
    ]

    return templates.TemplateResponse(request=request, name="teacher.html", context={"user": user, "my_tests": my_tests})

@app.post("/generate-test", response_class=HTMLResponse)
async def generate_test(
    request: Request, 
    text: str = Form(...),
    num_questions: int = Form(3),
    difficulty: str = Form("средний")
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login")

    prompt = f"""
    Проанализируй следующий конспект и составь по нему тест из {num_questions} вопросов с выбором ответа.
    Уровень сложности: {difficulty}.
    
    Конспект:
    {text}
    
    Верни ответ СТРОГО в формате валидного JSON без какого-либо лишнего текста, пояснений или разметки markdown:
    [
        {{
            "id": 1,
            "question": "Текст вопроса",
            "options": ["Вариант 1", "Вариант 2", "Вариант 3", "Вариант 4"],
            "correct": "Правильный вариант"
        }}
    ]
    """

    try:
        response = client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
        
        ai_raw_text = response.choices[0].message.content.strip()
        
        start_idx = ai_raw_text.find("[")
        end_idx = ai_raw_text.rfind("]")
        
        if start_idx != -1 and end_idx != -1:
            ai_raw_text = ai_raw_text[start_idx:end_idx + 1]

        questions_json = json.loads(ai_raw_text)
        test_id = str(uuid.uuid4())[:8]

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tests (id, teacher_username, questions_json) VALUES (?, ?, ?)", (test_id, user, json.dumps(questions_json, ensure_ascii=False)))
        conn.commit()
        conn.close()

        return templates.TemplateResponse(
            request=request, 
            name="test.html", 
            context={"questions": questions_json, "test_id": test_id}
        )

    except Exception as e:
        print(f"ОШИБКА ГЕНЕРАЦИИ: {e}")
        return RedirectResponse(url="/", status_code=303)

@app.get("/quiz/{test_id}", response_class=HTMLResponse)
async def student_quiz(request: Request, test_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT questions_json FROM tests WHERE id = ?", (test_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return HTMLResponse(content="<h1>Тест не найден</h1>", status_code=404)

    questions = json.loads(row[0])
    return templates.TemplateResponse(request=request, name="quiz.html", context={"questions": questions, "test_id": test_id})

@app.post("/submit-quiz/{test_id}", response_class=HTMLResponse)
async def submit_quiz(request: Request, test_id: str, student_name: str = Form(...)):
    form_data = await request.form()
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT questions_json FROM tests WHERE id = ?", (test_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return HTMLResponse(content="<h1>Тест не найден</h1>", status_code=404)

    questions = json.loads(row[0])
    correct_count = 0
    total_questions = len(questions)
    review = []

    for q in questions:
        user_ans = form_data.get(f"question_{q['id']}")
        is_correct = (user_ans == q["correct"])
        
        if is_correct:
            correct_count += 1
            
        review.append({
            "question": q["question"],
            "user_answer": user_ans,
            "correct_answer": q["correct"],
            "is_correct": is_correct
        })

    score_percent = int((correct_count / total_questions) * 100)

    cursor.execute("""
        INSERT INTO results (test_id, student_name, score, correct_count, total_questions)
        VALUES (?, ?, ?, ?, ?)
    """, (test_id, student_name, score_percent, correct_count, total_questions))
    
    conn.commit()
    conn.close()

    return templates.TemplateResponse(
        request=request, 
        name="result.html", 
        context={
            "student_name": student_name,
            "score": score_percent, 
            "correct_count": correct_count, 
            "total": total_questions,
            "review": review
        }
    )

@app.get("/stats/{test_id}", response_class=HTMLResponse)
async def view_stats(request: Request, test_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT student_name, score, correct_count, total_questions FROM results WHERE test_id = ?", (test_id,))
    rows = cursor.fetchall()
    conn.close()

    results_list = [
        {"student_name": r[0], "score": r[1], "correct_count": r[2], "total_questions": r[3]}
        for r in rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={"results": results_list, "test_id": test_id}
    )

if __name__ == "__main__":
    uvicorn.run("PythonApplication:app", host="127.0.0.1", port=8000, reload=True)