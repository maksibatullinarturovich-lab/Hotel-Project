from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta, timezone
import mysql.connector
from mysql.connector import errorcode
import os
from jose import JWTError, jwt

app = FastAPI(title="Hotel API (Single Hotel)", version="2.1.0")

# Настройки БД берутся из переменных окружения (в Docker их задаёт docker-compose.yml).
# Значения по умолчанию позволяют по-прежнему запускать API локально без Docker.
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "api"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "hotel_db_single"),
    "charset": "utf8mb4",
    "autocommit": False,
    "use_pure": True
}

SECRET_KEY = os.getenv("SECRET_KEY", "super_secret_key_change_in_prod")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 1440
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")


class UserRegister(BaseModel):
    login: str
    password: str
    passport_number: str
    last_name: str
    first_name: str
    middle_name: Optional[str] = None


class LoginRequest(BaseModel):
    login: str
    password: str


class BookingCreate(BaseModel):
    room_number: int
    check_in: str
    check_out: str


class ServiceAdd(BaseModel):
    service_id: int
    service_date: str


def get_db():
    conn = mysql.connector.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


def create_access_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode = data.copy()
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme), db=Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        login = payload.get("sub")
        if not login: raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    cur = db.cursor(dictionary=True)
    cur.execute("SELECT guest_id, login, last_name, first_name, role FROM guests WHERE login = %s", (login,))
    user = cur.fetchone()
    if not user: raise HTTPException(status_code=401, detail="User not found")
    return user


@app.post("/register", status_code=status.HTTP_201_CREATED)
def register(user: UserRegister, db=Depends(get_db)):
    cur = db.cursor()
    cur.execute("SELECT 1 FROM guests WHERE login = %s OR passport_number = %s", (user.login, user.passport_number))
    if cur.fetchone(): return JSONResponse(status_code=400, content={"msg": "Логин или паспорт уже существуют"})
    cur.execute(
        "INSERT INTO guests (login, password, passport_number, last_name, first_name, middle_name, role) VALUES (%s, %s, %s, %s, %s, %s, 'guest')",
        (user.login, user.password, user.passport_number, user.last_name, user.first_name, user.middle_name))
    db.commit()
    return {"msg": "Гость успешно зарегистрирован"}


@app.post("/login")
def login(credentials: LoginRequest, db=Depends(get_db)):
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT guest_id, login, password, role FROM guests WHERE login = %s", (credentials.login,))
    db_user = cur.fetchone()
    if not db_user or db_user["password"] != credentials.password:
        return JSONResponse(status_code=401, content={"msg": "Неверный логин или пароль"})
    token = create_access_token(
        data={"sub": db_user["login"], "guest_id": db_user["guest_id"], "role": db_user.get("role", "guest")})
    return {"access_token": token, "token_type": "bearer", "role": db_user.get("role", "guest")}


@app.get("/profile")
def get_profile(current_user: dict = Depends(get_current_user), db=Depends(get_db)):
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT guest_id, login, last_name, first_name, middle_name, passport_number FROM guests WHERE login = %s",
        (current_user["login"],))
    return cur.fetchone()


@app.get("/guests/current")
def get_current_guests(db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT g.last_name AS `Фамилия`, g.first_name AS `Имя`, b.room_number AS `Номер_комнаты`, DATE_FORMAT(b.check_in, '%d.%m.%Y') AS `Дата_заезда`, DATE_FORMAT(b.check_out, '%d.%m.%Y') AS `Дата_выезда`, 'Гранд Отель' AS `Гостиница` FROM bookings b JOIN guests g ON b.guest_id = g.guest_id WHERE CURDATE() BETWEEN b.check_in AND b.check_out AND b.status != 'Отменено' ORDER BY b.room_number")
    return cur.fetchall()


@app.get("/rooms/availability")
def get_room_availability(db=Depends(get_db)):
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM v_freerooms")
    rooms = cur.fetchall()
    cur.execute("SELECT * FROM v_freeseats")
    seats = cur.fetchall()
    return {"free_rooms": rooms, "free_seats": seats}


@app.get("/rooms/search")
def search_available_rooms(check_in: str, check_out: str, db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT r.room_number, rc.category_name AS class, r.capacity, rc.base_price_per_night FROM rooms r JOIN room_categories rc ON r.category_id = rc.category_id ORDER BY r.room_number")
    all_rooms = cur.fetchall()
    cur.execute(
        "SELECT DISTINCT room_number FROM bookings WHERE status != 'Отменено' AND %s < check_out AND %s > check_in",
        (check_in, check_out))
    busy_rooms = {row['room_number'] for row in cur.fetchall()}
    available = [
        {"Класс_номера": r['class'], "Номер_комнаты": r['room_number'], "Общее_мест": r['capacity'], "Занято_мест": 0,
         "Свободно_мест": r['capacity'], "Цена_за_ночь": float(r['base_price_per_night'])} for r in all_rooms if
        r['room_number'] not in busy_rooms]
    return {"available_rooms": available}


@app.get("/services")
def get_services(db=Depends(get_db)):
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT service_id, name, category, type, price FROM services")
    return cur.fetchall()


@app.get("/bookings/my")
def get_my_bookings(db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT b.booking_id, b.room_number, b.check_in, b.check_out, b.status, rc.category_name AS room_class, rc.base_price_per_night FROM bookings b JOIN rooms r ON b.room_number = r.room_number JOIN room_categories rc ON r.category_id = rc.category_id WHERE b.guest_id = %s AND b.status != 'Отменено' ORDER BY b.booking_id DESC",
        (user["guest_id"],))
    return cur.fetchall()


@app.get("/bookings/invoices")
def get_all_invoices(db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    role = user.get("role", "guest")
    query = """
        SELECT b.booking_id AS `Номер_брони`, 
               g.last_name AS `Фамилия`, g.first_name AS `Имя`,
               b.room_number, b.check_in, b.check_out, b.status,
               rc.category_name AS room_class
        FROM bookings b 
        JOIN guests g ON b.guest_id = g.guest_id
        JOIN rooms r ON b.room_number = r.room_number
        JOIN room_categories rc ON r.category_id = rc.category_id
        WHERE b.status != 'Отменено'
    """
    params = []
    if role != "admin":
        query += " AND b.guest_id = %s"
        params.append(user["guest_id"])
    query += " ORDER BY b.booking_id DESC"
    cur.execute(query, tuple(params))
    return cur.fetchall()

@app.get("/bookings/{booking_id}/invoice")
def get_invoice(booking_id: int, db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT b.booking_id, g.last_name AS guest_surname, b.status, DATEDIFF(b.check_out, b.check_in) AS nights, rc.base_price_per_night, (DATEDIFF(b.check_out, b.check_in) * rc.base_price_per_night) AS stay_cost FROM bookings b JOIN guests g ON b.guest_id = g.guest_id JOIN rooms r ON b.room_number = r.room_number JOIN room_categories rc ON r.category_id = rc.category_id WHERE b.booking_id = %s",
        (booking_id,))
    base = cur.fetchone()
    if not base: return JSONResponse(status_code=404, content={"msg": "Бронирование не найдено"})
    cur.execute(
        "SELECT COALESCE(SUM(rs.recorded_price), 0) AS services_cost FROM room_services rs WHERE rs.booking_id = %s",
        (booking_id,))
    services = cur.fetchone()
    return {"booking_id": base['booking_id'], "guest_surname": base['guest_surname'],
            "stay_cost": float(base['stay_cost']), "services_cost": float(services['services_cost']),
            "total": float(base['stay_cost']) + float(services['services_cost']), "status": base['status']}


@app.post("/bookings", status_code=status.HTTP_201_CREATED)
def create_booking(booking: BookingCreate, db=Depends(get_db), user: dict = Depends(get_current_user)):
    try:
        check_in_date = datetime.strptime(booking.check_in, "%Y-%m-%d").date()
        check_out_date = datetime.strptime(booking.check_out, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse(status_code=400, content={"msg": "Неверный формат даты"})

    if check_in_date < datetime.now(timezone.utc).date():
        return JSONResponse(status_code=400, content={"msg": "Дата заезда не может быть в прошлом"})
    if check_out_date <= check_in_date:
        return JSONResponse(status_code=400, content={"msg": "Дата выезда должна быть позже даты заезда"})

    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT COUNT(*) as cnt FROM bookings WHERE room_number = %s AND status != 'Отменено' AND %s < check_out AND %s > check_in",
        (booking.room_number, booking.check_in, booking.check_out))
    if cur.fetchone()["cnt"] > 0:
        return JSONResponse(status_code=400, content={"msg": "Номер уже занят на выбранные даты"})

    cur.execute("INSERT INTO bookings (guest_id, room_number, check_in, check_out) VALUES (%s, %s, %s, %s)",
                (user["guest_id"], booking.room_number, booking.check_in, booking.check_out))
    db.commit()
    return {"msg": "Бронирование успешно создано", "booking_id": cur.lastrowid}


@app.post("/bookings/{booking_id}/services", status_code=status.HTTP_201_CREATED)
def add_service(booking_id: int, service: ServiceAdd, db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT 1 FROM bookings WHERE booking_id = %s", (booking_id,))
    if not cur.fetchone():
        raise HTTPException(404, "Бронирование не найдено")

    cur.execute("SELECT price FROM services WHERE service_id = %s", (service.service_id,))
    svc = cur.fetchone()
    if not svc:
        raise HTTPException(400, "Услуга не найдена")

    try:
        cur.execute("""
            INSERT INTO room_services (booking_id, service_id, service_date, recorded_price) 
            VALUES (%s, %s, %s, %s)
        """, (booking_id, service.service_id, service.service_date, svc["price"]))
        db.commit()
        return {"msg": "Услуга успешно добавлена"}
    except Exception as err:
        db.rollback()
        if hasattr(err, 'errno') and err.errno == errorcode.ER_DUP_ENTRY:
            raise HTTPException(400, detail="Эта услуга уже добавлена в данное бронирование")
        raise HTTPException(500, detail="Ошибка сервера при добавлении услуги")


@app.get("/bookings/{booking_id}/check")
def get_service_check(booking_id: int, db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT rs.service_id AS `service_id`, s.name AS `Название_услуги`, rs.recorded_price AS `Цена`, rs.recorded_price AS `Итого_за_услугу`, rs.service_date AS `service_date` FROM room_services rs JOIN services s ON rs.service_id = s.service_id WHERE rs.booking_id = %s",
        (booking_id,))
    return cur.fetchall()


@app.put("/bookings/{booking_id}/services/{service_id}")
def update_service(booking_id: int, service_id: int, service: ServiceAdd, db=Depends(get_db),
                   user: dict = Depends(get_current_user)):
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT guest_id FROM bookings WHERE booking_id = %s", (booking_id,))
    b = cur.fetchone()
    if not b or b["guest_id"] != user["guest_id"]: return JSONResponse(status_code=403,
                                                                       content={"msg": "Доступ запрещён"})
    cur.execute("UPDATE room_services SET service_date = %s WHERE booking_id = %s AND service_id = %s",
                (service.service_date, booking_id, service_id))
    if cur.rowcount == 0: return JSONResponse(status_code=404, content={"msg": "Услуга не найдена"})
    db.commit()
    return {"msg": "Дата услуги обновлена"}


@app.delete("/bookings/{booking_id}/services/{service_id}")
def delete_service(booking_id: int, service_id: int, db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor()
    cur.execute("SELECT guest_id FROM bookings WHERE booking_id = %s", (booking_id,))
    b = cur.fetchone()
    if not b or b[0] != user["guest_id"]: return JSONResponse(status_code=403, content={"msg": "Доступ запрещён"})
    cur.execute("DELETE FROM room_services WHERE booking_id = %s AND service_id = %s", (booking_id, service_id))
    if cur.rowcount == 0: return JSONResponse(status_code=404, content={"msg": "Услуга не найдена"})
    db.commit()
    return {"msg": "Услуга удалена"}


@app.post("/bookings/{booking_id}/confirm-payment")
def confirm_payment(booking_id: int, db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor()
    cur.execute("UPDATE bookings SET status = 'Оплачено' WHERE booking_id = %s AND status = 'Ожидает оплаты'",
                (booking_id,))
    if cur.rowcount == 0: return JSONResponse(status_code=400,
                                              content={"msg": "Бронь не найдена или уже оплачена/отменена"})
    db.commit()
    return get_invoice(booking_id, db, user)


@app.post("/bookings/auto-cancel-unpaid")
def auto_cancel_unpaid_bookings(db=Depends(get_db)):
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT booking_id, room_number, check_in, guest_id FROM bookings WHERE status = 'Ожидает оплаты' AND check_in < CURDATE() - INTERVAL 1 DAY")
    expired = cur.fetchall()
    cancelled_count = 0
    for booking in expired:
        cur.execute("UPDATE bookings SET status = 'Отменено' WHERE booking_id = %s", (booking["booking_id"],))
        cancelled_count += 1
    db.commit()
    return {"msg": f"Авто-отменено броней: {cancelled_count}", "cancelled": cancelled_count}


@app.put("/profile/credentials")
def update_credentials(credentials: dict, db=Depends(get_db), user: dict = Depends(get_current_user)):
    cur = db.cursor()
    cur.execute("SELECT password FROM guests WHERE guest_id = %s", (user["guest_id"],))
    current_pass = cur.fetchone()[0]
    if current_pass != credentials.get("current_password"): return JSONResponse(status_code=400, content={
        "msg": "Неверный текущий пароль"})
    new_login = credentials.get("new_login", "").strip()
    if new_login and len(new_login) >= 3:
        cur.execute("SELECT 1 FROM guests WHERE login = %s AND guest_id != %s", (new_login, user["guest_id"]))
        if cur.fetchone(): return JSONResponse(status_code=400, content={"msg": "Этот логин уже занят"})
        cur.execute("UPDATE guests SET login = %s WHERE guest_id = %s", (new_login, user["guest_id"]))
    new_password = credentials.get("new_password", "").strip()
    if new_password and len(new_password) >= 4:
        cur.execute("UPDATE guests SET password = %s WHERE guest_id = %s", (new_password, user["guest_id"]))
    db.commit()
    return {"msg": "Данные успешно обновлены"}


@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(booking_id: int, db=Depends(get_db), user: dict = Depends(get_current_user)):
    from datetime import date
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT status, check_in, guest_id FROM bookings WHERE booking_id = %s", (booking_id,))
    booking = cur.fetchone()

    if not booking:
        raise HTTPException(404, "Бронирование не найдено")
    if booking["status"] == "Отменено":
        raise HTTPException(400, "Бронь уже отменена")
    if booking["check_in"] < date.today():
        raise HTTPException(400, "Нельзя отменить прошедшую бронь")

    if user.get("role") != "admin" and booking["guest_id"] != user["guest_id"]:
        raise HTTPException(403, "Доступ запрещён")

    cur.execute("UPDATE bookings SET status = 'Отменено' WHERE booking_id = %s", (booking_id,))
    db.commit()
    return {"msg": "Бронирование успешно отменено"}


if __name__ == "__main__":
    import uvicorn

    # 0.0.0.0 нужен, чтобы API был доступен снаружи контейнера
    uvicorn.run(app, host="0.0.0.0", port=3000, loop="asyncio")
