"""
feedback_service.py  ─  Centralized Feedback FastAPI Microservice
Port : 8003
Run  : uvicorn feedback_service:app --host 0.0.0.0 --port 8003

Architecture: [User/Admin] -> [Flask Proxy app.py] -> [This Service] -> [Oracle DB]
"""

import os
import sys
import uuid
import secrets
import smtplib
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    _boto3_available = True
except ImportError:
    _boto3_available = False

import uvicorn
import oracledb
from fastapi import FastAPI, HTTPException, Query, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from dotenv import load_dotenv

load_dotenv()

# ================= ORACLE DB CONFIG =================
ORACLE_HOST = os.getenv("ORACLE_HOST", "56.228.73.210")
ORACLE_PORT = int(os.getenv("ORACLE_PORT", "1521"))
ORACLE_SERVICE_NAME = os.getenv("ORACLE_SERVICE_NAME", "FREE")
ORACLE_USER = os.getenv("ORACLE_USER", "CHAKORA")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "Chakora##2026")

FEEDBACK_BASE_URL = os.getenv("FEEDBACK_BASE_URL", "http://localhost:8080/feedback").rstrip("/")

app = FastAPI(title="Centralized Feedback Service", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db_connection():
    """Returns a connection to Oracle Database."""
    try:
        dsn = oracledb.makedsn(
            host=ORACLE_HOST,
            port=ORACLE_PORT,
            service_name=ORACLE_SERVICE_NAME,
        )
        conn = oracledb.connect(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=dsn,
        )
        return conn
    except Exception as e:
        print(f"[ERROR] Oracle DB Connection Failed: {e}")
        return None


# Helper to convert Oracle cursor rows to dicts
def rows_to_dicts(cursor, rows):
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in rows]


# ================= AUDIT LOGGING =================
def log_audit_event(cursor, feedback_id: str, action: str, actor: str, details: str):
    try:
        log_id = f"LOG-{uuid.uuid4().hex[:12]}"
        cursor.execute(
            """
            INSERT INTO FEEDBACK_AUDIT_LOGS (LOG_ID, FEEDBACK_ID, ACTION, ACTOR, DETAILS, TIMESTAMP)
            VALUES (:1, :2, :3, :4, :5, CURRENT_TIMESTAMP)
            """,
            (log_id, feedback_id, action, actor, details)
        )
    except Exception as e:
        print(f"[WARNING] Failed to write audit log: {e}")


# ================= PYDANTIC MODELS =================
class ActivityRecordRequest(BaseModel):
    student_id: Optional[str] = None
    registration_id: Optional[str] = None
    email: str
    module_name: str
    activity_type: str
    activity_name: str
    reference_id: Optional[str] = None
    completed_flag: bool = True
    completed_date: Optional[str] = None
    remarks: Optional[str] = None


class GenerateLinkRequest(BaseModel):
    student_email: str
    registration_id: Optional[str] = None
    student_id: Optional[str] = None
    activity_ids: List[str]
    expiry_hours: float = 24.0
    generated_by: Optional[str] = "ADMIN"


class SendEmailRequest(BaseModel):
    feedback_id: str
    student_name: str
    student_email: str
    expiry_hours: float = 24.0


class SingleResponse(BaseModel):
    activity_id: Optional[str] = None
    question_id: str
    rating_value: Optional[float] = None
    text_answer: Optional[str] = None
    file_url: Optional[str] = None


class FeedbackSubmitRequest(BaseModel):
    token: str
    responses: List[SingleResponse]
    overall_rating: Optional[float] = None
    comments: Optional[str] = None


# ================= REUSABLE CORE HELPER =================
def record_activity_core(
    email: str,
    module_name: str,
    activity_type: str,
    activity_name: str,
    student_id: Optional[str] = None,
    registration_id: Optional[str] = None,
    reference_id: Optional[str] = None,
    completed_flag: bool = True,
    completed_date: Optional[datetime] = None,
    remarks: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Centralized Python helper function to record any activity from any module.
    Callable directly in Python or via POST /feedback/activity REST API.
    """
    conn = get_db_connection()
    if not conn:
        return {"success": False, "message": "Database connection failed"}

    try:
        cursor = conn.cursor()

        # Deduce student_id / registration_id if missing from NRM_USERS / NRM_STUDENTS / NRM_REGISTRATIONS
        if not registration_id or not student_id:
            cursor.execute(
                """
                SELECT r.REGISTRATION_ID, r.STUDENT_ID
                FROM NRM_REGISTRATIONS r
                JOIN NRM_STUDENTS s ON r.STUDENT_ID = s.ID
                JOIN NRM_USERS u ON s.USER_ID = u.ID
                WHERE LOWER(TRIM(u.EMAIL)) = LOWER(TRIM(:email))
                """,
                {"email": email}
            )
            row = cursor.fetchone()
            if row:
                if not registration_id:
                    registration_id = row[0]
                if not student_id:
                    student_id = str(row[1])

        # Evaluate Feedback Eligibility Rule
        # Activity becomes eligible ONLY when completed_flag is True
        is_completed = 1 if completed_flag else 0
        is_eligible = 1 if is_completed else 0

        activity_id = f"ACT-{uuid.uuid4().hex[:12]}"
        comp_date = completed_date or datetime.utcnow()

        cursor.execute(
            """
            INSERT INTO FEEDBACK_ACTIVITY (
                ACTIVITY_ID, STUDENT_ID, REGISTRATION_ID, EMAIL,
                MODULE_NAME, ACTIVITY_TYPE, ACTIVITY_NAME, REFERENCE_ID,
                COMPLETED_FLAG, COMPLETED_DATE, ELIGIBLE_FOR_FEEDBACK, FEEDBACK_SUBMITTED, REMARKS
            ) VALUES (
                :1, :2, :3, :4, :5, :6, :7, :8, :9, :10, :11, 0, :12
            )
            """,
            (
                activity_id, student_id, registration_id, email,
                module_name, activity_type, activity_name, reference_id,
                is_completed, comp_date, is_eligible, remarks
            )
        )
        conn.commit()

        log_audit_event(
            cursor,
            feedback_id="",
            action="RECORDED",
            actor="SYSTEM",
            details=f"Activity '{activity_name}' ({module_name}) recorded for {email}. Eligible: {bool(is_eligible)}"
        )
        conn.commit()

        cursor.close()
        conn.close()
        return {
            "success": True,
            "activity_id": activity_id,
            "eligible": bool(is_eligible),
            "message": "Activity recorded successfully"
        }
    except Exception as e:
        print(f"[ERROR] record_activity_core failed: {e}")
        if conn:
            conn.close()
        return {"success": False, "message": str(e)}


# ================= REST ENDPOINTS =================

@app.post("/feedback/activity")
async def api_record_activity(req: ActivityRecordRequest):
    """API Endpoint to record student activity from any microservice/module."""
    comp_dt = None
    if req.completed_date:
        try:
            comp_dt = datetime.fromisoformat(req.completed_date)
        except Exception:
            comp_dt = datetime.utcnow()

    res = record_activity_core(
        email=req.email,
        module_name=req.module_name,
        activity_type=req.activity_type,
        activity_name=req.activity_name,
        student_id=req.student_id,
        registration_id=req.registration_id,
        reference_id=req.reference_id,
        completed_flag=req.completed_flag,
        completed_date=comp_dt,
        remarks=req.remarks
    )
    if not res["success"]:
        raise HTTPException(status_code=500, detail=res["message"])
    return res


@app.post("/feedback/generate")
async def api_generate_link(req: GenerateLinkRequest):
    """Admin generates secure unique token for selected eligible activities with custom expiry."""
    if not req.activity_ids:
        raise HTTPException(status_code=400, detail="At least one activity must be selected.")

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = conn.cursor()
        feedback_id = f"FB-{uuid.uuid4().hex[:12]}"
        secure_token = f"{uuid.uuid4().hex}{secrets.token_hex(16)}"

        created_at = datetime.utcnow()
        expires_at = created_at + timedelta(hours=req.expiry_hours)

        # Insert Parent Record
        cursor.execute(
            """
            INSERT INTO FEEDBACK_MASTER (
                FEEDBACK_ID, STUDENT_ID, REGISTRATION_ID, EMAIL, TOKEN,
                TOKEN_CREATED_AT, TOKEN_EXPIRES_AT, EXPIRY_HOURS, OVERALL_STATUS,
                GENERATED_BY, CREATED_AT, UPDATED_AT
            ) VALUES (
                :1, :2, :3, :4, :5, :6, :7, :8, 'GENERATED', :9, :10, :11
            )
            """,
            (
                feedback_id, req.student_id, req.registration_id, req.student_email, secure_token,
                created_at, expires_at, req.expiry_hours, req.generated_by, created_at, created_at
            )
        )

        # Link selected activities
        for act_id in req.activity_ids:
            cursor.execute(
                """
                UPDATE FEEDBACK_ACTIVITY
                SET FEEDBACK_ID = :1, ELIGIBLE_FOR_FEEDBACK = 1
                WHERE ACTIVITY_ID = :2 AND EMAIL = :3
                """,
                (feedback_id, act_id, req.student_email)
            )

        log_audit_event(
            cursor,
            feedback_id=feedback_id,
            action="GENERATED",
            actor=req.generated_by or "ADMIN",
            details=f"Token generated for {len(req.activity_ids)} activities. Expiry: {req.expiry_hours} hours."
        )

        conn.commit()

        feedback_url = f"{FEEDBACK_BASE_URL}?token={secure_token}"

        cursor.close()
        conn.close()

        return {
            "success": True,
            "feedback_id": feedback_id,
            "token": secure_token,
            "feedback_url": feedback_url,
            "expires_at": expires_at.isoformat(),
            "expiry_hours": req.expiry_hours
        }
    except Exception as e:
        if conn:
            conn.close()
        print(f"[ERROR] api_generate_link failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/feedback/send")
async def api_send_email(req: SendEmailRequest):
    """Triggers feedback email containing secure link, student name, activities, and expiry."""
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = conn.cursor()

        # Fetch Master Record & Activities
        cursor.execute(
            """
            SELECT TOKEN, OVERALL_STATUS, TOKEN_EXPIRES_AT
            FROM FEEDBACK_MASTER
            WHERE FEEDBACK_ID = :1
            """,
            {"1": req.feedback_id}
        )
        m_row = cursor.fetchone()
        if not m_row:
            raise HTTPException(status_code=404, detail="Feedback request not found.")

        token, status, expires_at = m_row
        feedback_url = f"{FEEDBACK_BASE_URL}?token={token}"

        # Fetch linked activities
        cursor.execute(
            """
            SELECT MODULE_NAME, ACTIVITY_NAME
            FROM FEEDBACK_ACTIVITY
            WHERE FEEDBACK_ID = :1
            """,
            {"1": req.feedback_id}
        )
        act_rows = cursor.fetchall()
        activities_list_html = "".join([f"<li><strong>[{row[0]}]</strong> {row[1]}</li>" for row in act_rows])

        # Prepare Email Content
        subject = f"Feedback Request - ChakoraHub"
        body_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; background-color: #f4f6f9; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: #ffffff; padding: 28px; border-radius: 12px; border: 1px solid #e2e8f0;">
                <h2 style="color: #4f46e5; margin-top: 0;">We Value Your Feedback!</h2>
                <p>Dear <strong>{req.student_name}</strong>,</p>
                <p>Thank you for engaging with ChakoraHub! Please take a moment to share your feedback regarding your recent activities:</p>
                <ul style="line-height: 1.8; color: #334155;">
                    {activities_list_html}
                </ul>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{feedback_url}" style="background-color: #4f46e5; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 50px; font-weight: bold; display: inline-block;">
                        Complete Feedback Form
                    </a>
                </div>
                <p style="font-size: 13px; color: #e11d48;">
                    ⏳ <strong>Note:</strong> This feedback link is valid for <strong>{req.expiry_hours} hours</strong> and can only be used once.
                </p>
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 24px 0;">
                <p style="font-size: 12px; color: #64748b; text-align: center;">
                    Thank you,<br><strong>ChakoraHub Team</strong>
                </p>
            </div>
        </body>
        </html>
        """

        # --- Dispatch Email via AWS SES (primary) ---
        aws_access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
        aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
        ses_sender = os.getenv("SES_SENDER_EMAIL", "admin@chakorahub.com")
        aws_region = os.getenv("AWS_DEFAULT_REGION", "ap-south-1")

        sent_success = False

        if _boto3_available and aws_access_key and aws_secret_key:
            try:
                ses_client = boto3.client(
                    "ses",
                    region_name=aws_region,
                    aws_access_key_id=aws_access_key,
                    aws_secret_access_key=aws_secret_key,
                )
                ses_client.send_email(
                    Source=ses_sender,
                    Destination={"ToAddresses": [req.student_email]},
                    Message={
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Html": {"Data": body_html, "Charset": "UTF-8"}},
                    },
                )
                sent_success = True
                print(f"[AWS SES] Email dispatched to {req.student_email}")
            except Exception as ses_err:
                print(f"[WARNING] AWS SES dispatch failed: {ses_err}")

        # --- Fallback: SMTP ---
        if not sent_success:
            smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
            smtp_port = int(os.getenv("SMTP_PORT", "587"))
            smtp_user = os.getenv("SMTP_USER", ses_sender)
            smtp_pass = os.getenv("SMTP_PASSWORD", "")
            if smtp_pass:
                try:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = smtp_user
                    msg["To"] = req.student_email
                    msg.attach(MIMEText(body_html, "html"))
                    server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.send_message(msg)
                    server.quit()
                    sent_success = True
                    print(f"[SMTP] Email dispatched to {req.student_email}")
                except Exception as se:
                    print(f"[WARNING] SMTP dispatch failed: {se}")

        if not sent_success:
            print(f"[DEV EMAIL LOG] No email credentials configured. Link: {feedback_url}")
            sent_success = True

        # Update status in FEEDBACK_MASTER
        cursor.execute(
            """
            UPDATE FEEDBACK_MASTER
            SET OVERALL_STATUS = 'SENT', SENT_AT = CURRENT_TIMESTAMP, UPDATED_AT = CURRENT_TIMESTAMP
            WHERE FEEDBACK_ID = :1
            """,
            {"1": req.feedback_id}
        )

        log_audit_event(
            cursor,
            feedback_id=req.feedback_id,
            action="SENT",
            actor="SYSTEM",
            details=f"Email sent to {req.student_email} with URL: {feedback_url}"
        )

        conn.commit()
        cursor.close()
        conn.close()

        return {"success": True, "message": f"Email sent successfully to {req.student_email}"}
    except Exception as e:
        if conn:
            conn.close()
        print(f"[ERROR] api_send_email failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/feedback/status")
async def api_get_feedback_status_metrics():
    """Returns overall dashboard summary metrics for the Control Panel."""
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM FEEDBACK_ACTIVITY WHERE ELIGIBLE_FOR_FEEDBACK = 1")
        total_eligible = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM FEEDBACK_MASTER WHERE OVERALL_STATUS = 'PENDING' OR OVERALL_STATUS = 'GENERATED'")
        pending = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM FEEDBACK_MASTER WHERE OVERALL_STATUS = 'GENERATED' OR OVERALL_STATUS = 'SENT'")
        generated = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM FEEDBACK_MASTER WHERE OVERALL_STATUS = 'SUBMITTED'")
        submitted = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM FEEDBACK_MASTER WHERE OVERALL_STATUS = 'EXPIRED'")
        expired = cursor.fetchone()[0]

        cursor.execute("SELECT AVG(AVERAGE_RATING) FROM FEEDBACK_MASTER WHERE OVERALL_STATUS = 'SUBMITTED' AND AVERAGE_RATING IS NOT NULL")
        avg_rating_row = cursor.fetchone()[0]
        avg_rating = round(float(avg_rating_row), 2) if avg_rating_row else 0.0

        response_pct = round((submitted / generated * 100), 1) if generated > 0 else 0.0

        cursor.close()
        conn.close()

        return {
            "success": True,
            "total_eligible": total_eligible,
            "pending": pending,
            "generated": generated,
            "submitted": submitted,
            "expired": expired,
            "average_rating": avg_rating,
            "response_percentage": response_pct
        }
    except Exception as e:
        if conn:
            conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/feedback/report")
async def api_get_feedback_report():
    """Returns detailed feedback master list for student report control panel."""
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT m.FEEDBACK_ID, m.STUDENT_ID, m.REGISTRATION_ID, m.EMAIL,
                   m.TOKEN, m.TOKEN_CREATED_AT, m.TOKEN_EXPIRES_AT, m.EXPIRY_HOURS,
                   m.OVERALL_STATUS, m.SENT_AT, m.SUBMITTED_AT, m.AVERAGE_RATING,
                   s.FIRST_NAME, s.LAST_NAME, s.PHONE, s.ADDRESS
            FROM FEEDBACK_MASTER m
            LEFT JOIN NRM_STUDENTS s ON m.STUDENT_ID = s.ID
            ORDER BY m.CREATED_AT DESC
            """
        )
        reports = rows_to_dicts(cursor, cursor.fetchall())

        cursor.close()
        conn.close()
        return {"success": True, "reports": reports}
    except Exception as e:
        if conn:
            conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/feedback/student/{email}")
async def api_get_student_feedback_history(email: str):
    """Returns activity records and feedback eligibility for a specific student."""
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ACTIVITY_ID, MODULE_NAME, ACTIVITY_TYPE, ACTIVITY_NAME,
                   REFERENCE_ID, COMPLETED_FLAG, COMPLETED_DATE, ELIGIBLE_FOR_FEEDBACK,
                   FEEDBACK_SUBMITTED, RATING, REMARKS, CREATED_AT, FEEDBACK_ID
            FROM FEEDBACK_ACTIVITY
            WHERE LOWER(TRIM(EMAIL)) = LOWER(TRIM(:1))
            ORDER BY CREATED_AT DESC
            """,
            {"1": email}
        )
        activities = rows_to_dicts(cursor, cursor.fetchall())

        cursor.close()
        conn.close()
        return {"success": True, "email": email, "activities": activities}
    except Exception as e:
        if conn:
            conn.close()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/feedback/{token}")
async def api_get_feedback_by_token(token: str):
    """
    Validates token, enforces one-time use & expiry rules,
    returns student details, linked activities, and dynamic questions.
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT FEEDBACK_ID, STUDENT_ID, REGISTRATION_ID, EMAIL, OVERALL_STATUS, TOKEN_EXPIRES_AT
            FROM FEEDBACK_MASTER
            WHERE TOKEN = :1
            """,
            {"1": token}
        )
        row = cursor.fetchone()
        if not row:
            cursor.close()
            conn.close()
            return JSONResponse(
                status_code=404,
                content={"success": False, "status": "INVALID", "message": "Invalid or non-existent feedback token."}
            )

        feedback_id, student_id, reg_id, email, status, expires_at = row

        # Check One-Time Link / Submitted
        if status == 'SUBMITTED':
            cursor.close()
            conn.close()
            return JSONResponse(
                status_code=400,
                content={"success": False, "status": "SUBMITTED", "message": "Feedback has already been submitted for this link. Thank you!"}
            )

        # Check Expiry
        now = datetime.utcnow()
        if expires_at and now > expires_at:
            cursor.execute(
                "UPDATE FEEDBACK_MASTER SET OVERALL_STATUS = 'EXPIRED', UPDATED_AT = CURRENT_TIMESTAMP WHERE FEEDBACK_ID = :1",
                {"1": feedback_id}
            )
            log_audit_event(cursor, feedback_id, "EXPIRED", "SYSTEM", "Token accessed after expiration time.")
            conn.commit()
            cursor.close()
            conn.close()
            return JSONResponse(
                status_code=410,
                content={"success": False, "status": "EXPIRED", "message": "This feedback link has expired."}
            )

        # Update Status to OPENED if GENERATED or SENT
        if status in ['GENERATED', 'SENT']:
            cursor.execute(
                "UPDATE FEEDBACK_MASTER SET OVERALL_STATUS = 'OPENED', UPDATED_AT = CURRENT_TIMESTAMP WHERE FEEDBACK_ID = :1",
                {"1": feedback_id}
            )
            log_audit_event(cursor, feedback_id, "OPENED", "STUDENT", "Student opened feedback link.")
            conn.commit()

        # Fetch Activities
        cursor.execute(
            """
            SELECT ACTIVITY_ID, MODULE_NAME, ACTIVITY_TYPE, ACTIVITY_NAME
            FROM FEEDBACK_ACTIVITY
            WHERE FEEDBACK_ID = :1
            """,
            {"1": feedback_id}
        )
        act_rows = rows_to_dicts(cursor, cursor.fetchall())
        modules_set = list(set([a["MODULE_NAME"] for a in act_rows] + ["ALL"]))

        # Fetch Questions for these modules
        query_in = ", ".join([f"'{m}'" for m in modules_set])
        cursor.execute(
            f"""
            SELECT QUESTION_ID, MODULE_NAME, QUESTION_TEXT, QUESTION_TYPE, IS_REQUIRED, DISPLAY_ORDER
            FROM FEEDBACK_QUESTIONS
            WHERE MODULE_NAME IN ({query_in})
            ORDER BY DISPLAY_ORDER ASC
            """
        )
        q_rows = rows_to_dicts(cursor, cursor.fetchall())

        cursor.close()
        conn.close()

        return {
            "success": True,
            "status": "VALID",
            "feedback_id": feedback_id,
            "student_email": email,
            "registration_id": reg_id,
            "activities": act_rows,
            "questions": q_rows
        }
    except Exception as e:
        if conn:
            conn.close()
        print(f"[ERROR] api_get_feedback_by_token failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/feedback/submit")
async def api_submit_feedback(req: FeedbackSubmitRequest):
    """
    Submits student feedback, updates ratings, marks token SUBMITTED (preventing reuse).
    """
    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=500, detail="Database connection failed")

    try:
        cursor = conn.cursor()

        # Fetch Master Record
        cursor.execute(
            """
            SELECT FEEDBACK_ID, EMAIL, OVERALL_STATUS, TOKEN_EXPIRES_AT
            FROM FEEDBACK_MASTER
            WHERE TOKEN = :1
            """,
            {"1": req.token}
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Invalid token.")

        feedback_id, email, status, expires_at = row

        if status == 'SUBMITTED':
            raise HTTPException(status_code=400, detail="Feedback already submitted.")

        if expires_at and datetime.utcnow() > expires_at:
            raise HTTPException(status_code=410, detail="Feedback link has expired.")

        # Save individual question responses
        ratings = []
        for r in req.responses:
            resp_id = f"RSP-{uuid.uuid4().hex[:12]}"
            cursor.execute(
                """
                INSERT INTO FEEDBACK_RESPONSES (
                    RESPONSE_ID, FEEDBACK_ID, ACTIVITY_ID, QUESTION_ID,
                    RATING_VALUE, TEXT_ANSWER, FILE_URL, SUBMITTED_AT
                ) VALUES (
                    :1, :2, :3, :4, :5, :6, :7, CURRENT_TIMESTAMP
                )
                """,
                (
                    resp_id, feedback_id, r.activity_id, r.question_id,
                    r.rating_value, r.text_answer, r.file_url
                )
            )
            if r.rating_value is not None:
                ratings.append(r.rating_value)

        avg_rating = req.overall_rating
        if avg_rating is None and ratings:
            avg_rating = round(sum(ratings) / len(ratings), 2)

        # Update Master Status to SUBMITTED
        cursor.execute(
            """
            UPDATE FEEDBACK_MASTER
            SET OVERALL_STATUS = 'SUBMITTED', SUBMITTED_AT = CURRENT_TIMESTAMP,
                AVERAGE_RATING = :1, UPDATED_AT = CURRENT_TIMESTAMP
            WHERE FEEDBACK_ID = :2
            """,
            (avg_rating, feedback_id)
        )

        # Update linked activities
        cursor.execute(
            """
            UPDATE FEEDBACK_ACTIVITY
            SET FEEDBACK_SUBMITTED = 1, RATING = :1, REMARKS = :2
            WHERE FEEDBACK_ID = :3
            """,
            (avg_rating, req.comments or "Submitted via form", feedback_id)
        )

        log_audit_event(
            cursor,
            feedback_id=feedback_id,
            action="SUBMITTED",
            actor="STUDENT",
            details=f"Student submitted feedback. Avg rating: {avg_rating}"
        )

        conn.commit()
        cursor.close()
        conn.close()

        return {"success": True, "message": "Feedback submitted successfully! Thank you."}

    except HTTPException:
        if conn:
            conn.close()
        raise
    except Exception as e:
        if conn:
            conn.close()
        print(f"[ERROR] api_submit_feedback failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
