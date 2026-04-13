from __future__ import annotations

import json
import math
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "guardian-track-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///guardian_track_app.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)
UTC = timezone.utc
SIMULATOR_STARTED = False


class Caregiver(db.Model):
    __tablename__ = "caregivers"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    patients = db.relationship("Patient", back_populates="caregiver", cascade="all, delete-orphan")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Patient(db.Model):
    __tablename__ = "patients"
    id = db.Column(db.Integer, primary_key=True)
    caregiver_id = db.Column(db.Integer, db.ForeignKey("caregivers.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    notes = db.Column(db.Text, nullable=False, default="")
    safe_zone_name = db.Column(db.String(120), nullable=False)
    safe_zone_lat = db.Column(db.Float, nullable=False)
    safe_zone_lng = db.Column(db.Float, nullable=False)
    geofence_radius_m = db.Column(db.Integer, nullable=False, default=100)
    pulse_min = db.Column(db.Integer, nullable=False, default=55)
    pulse_max = db.Column(db.Integer, nullable=False, default=120)
    temperature_max = db.Column(db.Float, nullable=False, default=37.8)
    spo2_min = db.Column(db.Integer, nullable=False, default=94)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    caregiver = db.relationship("Caregiver", back_populates="patients")
    telemetry = db.relationship("Telemetry", back_populates="patient", cascade="all, delete-orphan")
    alerts = db.relationship("Alert", back_populates="patient", cascade="all, delete-orphan")


class Telemetry(db.Model):
    __tablename__ = "telemetry"
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    pulse_rate = db.Column(db.Integer, nullable=False)
    body_temperature = db.Column(db.Float, nullable=False)
    spo2 = db.Column(db.Integer, nullable=False)
    fall_detected = db.Column(db.Boolean, nullable=False, default=False)
    battery_level = db.Column(db.Integer, nullable=False, default=100)

    patient = db.relationship("Patient", back_populates="telemetry")


class Alert(db.Model):
    __tablename__ = "alerts"
    id = db.Column(db.Integer, primary_key=True)
    patient_id = db.Column(db.Integer, db.ForeignKey("patients.id"), nullable=False)
    event_type = db.Column(db.String(60), nullable=False)
    severity = db.Column(db.String(30), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))

    patient = db.relationship("Patient", back_populates="alerts")


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "caregiver_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


def current_caregiver():
    caregiver_id = session.get("caregiver_id")
    return db.session.get(Caregiver, caregiver_id) if caregiver_id else None


def format_dt(value):
    if not value:
        return "-"
    return value.astimezone(UTC).strftime("%d %b %Y %H:%M UTC")


app.jinja_env.filters["datetime"] = format_dt


def haversine_distance_m(lat1, lon1, lat2, lon2):
    radius = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return radius * c


def latest_telemetry(patient):
    return Telemetry.query.filter_by(patient_id=patient.id).order_by(Telemetry.timestamp.desc(), Telemetry.id.desc()).first()


def recent_history(patient, limit=20):
    rows = Telemetry.query.filter_by(patient_id=patient.id).order_by(Telemetry.timestamp.desc(), Telemetry.id.desc()).limit(limit).all()
    return list(reversed(rows))


def patient_payload(patient, history_limit=20):
    latest = latest_telemetry(patient)
    history = recent_history(patient, history_limit)
    distance = None
    alerts = []
    status = "offline"

    if latest:
        distance = round(haversine_distance_m(latest.latitude, latest.longitude, patient.safe_zone_lat, patient.safe_zone_lng), 1)
        if latest.fall_detected:
            alerts.append("Fall detected")
        if latest.pulse_rate < patient.pulse_min or latest.pulse_rate > patient.pulse_max:
            alerts.append("Pulse abnormal")
        if latest.body_temperature > patient.temperature_max:
            alerts.append("Temperature high")
        if latest.spo2 < patient.spo2_min:
            alerts.append("SpO2 low")
        if distance > patient.geofence_radius_m:
            alerts.append("Outside safe zone")

        if latest.fall_detected or distance > patient.geofence_radius_m:
            status = "critical"
        elif alerts:
            status = "warning"
        else:
            status = "stable"

    return {
        "id": patient.id,
        "name": patient.name,
        "age": patient.age,
        "category": patient.category,
        "notes": patient.notes,
        "safe_zone_name": patient.safe_zone_name,
        "safe_zone_lat": patient.safe_zone_lat,
        "safe_zone_lng": patient.safe_zone_lng,
        "geofence_radius_m": patient.geofence_radius_m,
        "pulse_min": patient.pulse_min,
        "pulse_max": patient.pulse_max,
        "temperature_max": patient.temperature_max,
        "spo2_min": patient.spo2_min,
        "status": status,
        "distance_m": distance,
        "alerts": alerts,
        "latest": None if not latest else {
            "timestamp": latest.timestamp.isoformat(),
            "latitude": latest.latitude,
            "longitude": latest.longitude,
            "pulse_rate": latest.pulse_rate,
            "body_temperature": latest.body_temperature,
            "spo2": latest.spo2,
            "fall_detected": latest.fall_detected,
            "battery_level": latest.battery_level,
        },
        "history": [{
            "timestamp": row.timestamp.isoformat(),
            "latitude": row.latitude,
            "longitude": row.longitude,
            "pulse_rate": row.pulse_rate,
            "body_temperature": row.body_temperature,
            "spo2": row.spo2,
            "fall_detected": row.fall_detected,
            "battery_level": row.battery_level,
        } for row in history]
    }


def create_alert(patient, event_type, severity, message):
    db.session.add(Alert(patient_id=patient.id, event_type=event_type, severity=severity, message=message, created_at=datetime.now(UTC)))


def add_telemetry(patient, latitude, longitude, pulse_rate, body_temperature, spo2, battery_level, fall_detected=False, timestamp=None):
    row = Telemetry(
        patient_id=patient.id,
        latitude=latitude,
        longitude=longitude,
        pulse_rate=pulse_rate,
        body_temperature=body_temperature,
        spo2=spo2,
        battery_level=battery_level,
        fall_detected=fall_detected,
        timestamp=timestamp or datetime.now(UTC),
    )
    db.session.add(row)
    db.session.flush()

    distance = haversine_distance_m(latitude, longitude, patient.safe_zone_lat, patient.safe_zone_lng)
    if fall_detected:
        create_alert(patient, "fall", "critical", f"Fall detected for {patient.name}.")
    if distance > patient.geofence_radius_m:
        create_alert(patient, "geofence", "critical", f"{patient.name} moved outside the safe zone.")
    if pulse_rate < patient.pulse_min or pulse_rate > patient.pulse_max:
        create_alert(patient, "pulse", "warning", f"{patient.name} has abnormal pulse rate: {pulse_rate} bpm.")
    if body_temperature > patient.temperature_max:
        create_alert(patient, "temperature", "warning", f"{patient.name} has high temperature: {body_temperature} C.")
    if spo2 < patient.spo2_min:
        create_alert(patient, "spo2", "warning", f"{patient.name} has low SpO2: {spo2}%.")

    db.session.commit()
    return row


def dashboard_summary(caregiver):
    patients = Patient.query.filter_by(caregiver_id=caregiver.id).order_by(Patient.name).all()
    data = [patient_payload(p, 12) for p in patients]
    recent_alerts = Alert.query.join(Patient).filter(Patient.caregiver_id == caregiver.id).order_by(Alert.created_at.desc(), Alert.id.desc()).limit(6).all()
    return {
        "patients": data,
        "total_patients": len(data),
        "critical": sum(1 for x in data if x["status"] == "critical"),
        "warning": sum(1 for x in data if x["status"] == "warning"),
        "stable": sum(1 for x in data if x["status"] == "stable"),
        "recent_alerts": recent_alerts,
    }


def ensure_seed_data():
    db.create_all()

    if Caregiver.query.count() == 0:
        caregiver = Caregiver(full_name="Project Admin", email="caregiver@guardiantrack.com")
        caregiver.set_password("care123")
        db.session.add(caregiver)
        db.session.flush()

        patients = [
            Patient(
                caregiver_id=caregiver.id,
                name="Anita Rao",
                age=74,
                category="Alzheimer's Patient",
                notes="Evening walking risk is higher.",
                safe_zone_name="Green Park Home",
                safe_zone_lat=12.9719,
                safe_zone_lng=77.5937,
                geofence_radius_m=100,
                pulse_min=58,
                pulse_max=115,
                temperature_max=37.7,
                spo2_min=94,
            ),
            Patient(
                caregiver_id=caregiver.id,
                name="Kabir Sharma",
                age=11,
                category="Minor",
                notes="Track school commute.",
                safe_zone_name="Sunrise School Gate",
                safe_zone_lat=28.6139,
                safe_zone_lng=77.2090,
                geofence_radius_m=100,
                pulse_min=60,
                pulse_max=125,
                temperature_max=37.8,
                spo2_min=95,
            ),
        ]
        db.session.add_all(patients)
        db.session.commit()

    if Telemetry.query.count() == 0:
        demo = {
            "Anita Rao": [
                (12.9719, 77.5937, 82, 36.5, 98, 84, False),
                (12.9721, 77.5939, 85, 36.6, 97, 82, False),
                (12.9724, 77.5942, 87, 36.7, 97, 80, False),
            ],
            "Kabir Sharma": [
                (28.6139, 77.2090, 92, 36.7, 99, 91, False),
                (28.6140, 77.2092, 95, 36.8, 99, 90, False),
                (28.6143, 77.2094, 97, 36.8, 98, 89, False),
            ],
        }
        for patient in Patient.query.all():
            start = datetime.now(UTC) - timedelta(minutes=30)
            for i, p in enumerate(demo[patient.name]):
                add_telemetry(patient, p[0], p[1], p[2], p[3], p[4], p[5], p[6], start + timedelta(minutes=i * 10))


def start_simulator():
    global SIMULATOR_STARTED
    if SIMULATOR_STARTED:
        return
    SIMULATOR_STARTED = True

    def simulator():
        with app.app_context():
            while True:
                patients = Patient.query.all()
                if patients:
                    patient = random.choice(patients)
                    latest = latest_telemetry(patient)
                    if latest:
                        add_telemetry(
                            patient,
                            round(latest.latitude + random.uniform(-0.0003, 0.0003), 6),
                            round(latest.longitude + random.uniform(-0.0003, 0.0003), 6),
                            max(45, min(140, latest.pulse_rate + random.randint(-5, 5))),
                            round(max(36.0, min(38.5, latest.body_temperature + random.uniform(-0.2, 0.2))), 1),
                            max(88, min(100, latest.spo2 + random.randint(-1, 1))),
                            max(10, latest.battery_level - random.randint(0, 1)),
                            random.random() < 0.04,
                        )
                time.sleep(12)

    threading.Thread(target=simulator, daemon=True).start()


@app.context_processor
def inject_common():
    caregiver = current_caregiver()
    return {"logged_in_user": caregiver}


@app.route("/")
@login_required
def dashboard():
    caregiver = current_caregiver()
    summary = dashboard_summary(caregiver)
    selected_patient = summary["patients"][0] if summary["patients"] else None
    return render_template("dashboard.html", summary=summary, selected_patient=selected_patient, page="dashboard")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_caregiver():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        caregiver = Caregiver.query.filter(func.lower(Caregiver.email) == email).first()
        if caregiver and caregiver.check_password(password):
            session["caregiver_id"] = caregiver.id
            return redirect(url_for("dashboard"))
        flash("Invalid caregiver email or password.", "error")
    return render_template("login.html", page="login")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_caregiver():
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not full_name or not email or len(password) < 6:
            flash("Enter all details and use password of at least 6 characters.", "error")
        elif Caregiver.query.filter(func.lower(Caregiver.email) == email).first():
            flash("Email already exists.", "error")
        else:
            caregiver = Caregiver(full_name=full_name, email=email)
            caregiver.set_password(password)
            db.session.add(caregiver)
            db.session.commit()
            flash("Account created. Please log in.", "success")
            return redirect(url_for("login"))
    return render_template("signup.html", page="signup")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/patients", methods=["GET", "POST"])
@login_required
def patients_page():
    caregiver = current_caregiver()
    if request.method == "POST":
        patient = Patient(
            caregiver_id=caregiver.id,
            name=request.form["name"].strip(),
            age=int(request.form["age"]),
            category=request.form["category"].strip(),
            notes=request.form.get("notes", "").strip(),
            safe_zone_name=request.form["safe_zone_name"].strip(),
            safe_zone_lat=float(request.form["safe_zone_lat"]),
            safe_zone_lng=float(request.form["safe_zone_lng"]),
            geofence_radius_m=int(request.form["geofence_radius_m"]),
            pulse_min=int(request.form["pulse_min"]),
            pulse_max=int(request.form["pulse_max"]),
            temperature_max=float(request.form["temperature_max"]),
            spo2_min=int(request.form["spo2_min"]),
        )
        db.session.add(patient)
        db.session.commit()
        add_telemetry(patient, patient.safe_zone_lat, patient.safe_zone_lng, max(70, patient.pulse_min + 5), 36.7, max(96, patient.spo2_min), 100, False)
        flash("Patient added successfully.", "success")
        return redirect(url_for("patients_page"))

    patients = Patient.query.filter_by(caregiver_id=caregiver.id).order_by(Patient.name).all()
    return render_template("patients.html", patients=patients, page="patients")


@app.route("/geofencing")
@app.route("/geofencing/<int:patient_id>")
@login_required
def geofencing_page(patient_id=None):
    caregiver = current_caregiver()
    patients = Patient.query.filter_by(caregiver_id=caregiver.id).order_by(Patient.name).all()
    if not patients:
        return render_template("geofencing.html", patient=None, patient_json="{}", page="geofencing")
    selected = next((p for p in patients if p.id == patient_id), patients[0])
    return render_template("geofencing.html", patient=selected, patient_json=json.dumps(patient_payload(selected, 16)), page="geofencing")


@app.route("/health-history")
@app.route("/health-history/<int:patient_id>")
@login_required
def health_history_page(patient_id=None):
    caregiver = current_caregiver()
    patients = Patient.query.filter_by(caregiver_id=caregiver.id).order_by(Patient.name).all()
    if not patients:
        return render_template("health_history.html", patient=None, history_json="[]", page="health")
    selected = next((p for p in patients if p.id == patient_id), patients[0])
    payload = patient_payload(selected, 24)
    return render_template("health_history.html", patient=selected, patient_data=payload, history_json=json.dumps(payload["history"]), page="health")


@app.route("/alerts")
@login_required
def alerts_page():
    caregiver = current_caregiver()
    alerts = Alert.query.join(Patient).filter(Patient.caregiver_id == caregiver.id).order_by(Alert.created_at.desc(), Alert.id.desc()).all()
    return render_template("alerts.html", alerts=alerts, page="alerts")


@app.get("/api/patient/<int:patient_id>")
@login_required
def patient_api(patient_id):
    patient = db.session.get(Patient, patient_id)
    caregiver = current_caregiver()
    if patient is None or patient.caregiver_id != caregiver.id:
        return jsonify({"error": "Patient not found"}), 404
    return jsonify(patient_payload(patient, 24))


with app.app_context():
    ensure_seed_data()
    start_simulator()

if __name__ == "__main__":
    app.run(debug=True)
