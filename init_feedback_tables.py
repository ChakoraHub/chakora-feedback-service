import os
import sys
import oracledb
from dotenv import load_dotenv

load_dotenv()

ORACLE_HOST = os.getenv("ORACLE_HOST", "56.228.73.210")
ORACLE_PORT = int(os.getenv("ORACLE_PORT", "1521"))
ORACLE_SERVICE_NAME = os.getenv("ORACLE_SERVICE_NAME", "FREE")
ORACLE_USER = os.getenv("ORACLE_USER", "system")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "Chakora##2026")

def init_tables():
    print(f"Connecting to Oracle DB at {ORACLE_HOST}:{ORACLE_PORT}/{ORACLE_SERVICE_NAME} as {ORACLE_USER}...")
    try:
        dsn = oracledb.makedsn(
            host=ORACLE_HOST,
            port=ORACLE_PORT,
            service_name=ORACLE_SERVICE_NAME
        )
        conn = oracledb.connect(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=dsn
        )
        cursor = conn.cursor()
        print("Connected successfully!")

        tables = {
            "FEEDBACK_MASTER": """
                CREATE TABLE FEEDBACK_MASTER (
                    FEEDBACK_ID VARCHAR2(64) PRIMARY KEY,
                    STUDENT_ID VARCHAR2(64),
                    REGISTRATION_ID VARCHAR2(64),
                    EMAIL VARCHAR2(255),
                    TOKEN VARCHAR2(128) UNIQUE,
                    TOKEN_CREATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    TOKEN_EXPIRES_AT TIMESTAMP,
                    EXPIRY_HOURS NUMBER(5, 2) DEFAULT 24,
                    OVERALL_STATUS VARCHAR2(32) DEFAULT 'PENDING',
                    GENERATED_BY VARCHAR2(255),
                    SENT_AT TIMESTAMP,
                    SUBMITTED_AT TIMESTAMP,
                    AVERAGE_RATING NUMBER(5, 2),
                    CREATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UPDATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
            "FEEDBACK_ACTIVITY": """
                CREATE TABLE FEEDBACK_ACTIVITY (
                    ACTIVITY_ID VARCHAR2(64) PRIMARY KEY,
                    FEEDBACK_ID VARCHAR2(64),
                    STUDENT_ID VARCHAR2(64),
                    REGISTRATION_ID VARCHAR2(64),
                    EMAIL VARCHAR2(255),
                    MODULE_NAME VARCHAR2(64),
                    ACTIVITY_TYPE VARCHAR2(64),
                    ACTIVITY_NAME VARCHAR2(255),
                    REFERENCE_ID VARCHAR2(128),
                    COMPLETED_FLAG NUMBER(1) DEFAULT 0,
                    COMPLETED_DATE TIMESTAMP,
                    ELIGIBLE_FOR_FEEDBACK NUMBER(1) DEFAULT 0,
                    FEEDBACK_SUBMITTED NUMBER(1) DEFAULT 0,
                    RATING NUMBER(5, 2),
                    REMARKS VARCHAR2(4000),
                    CREATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT FK_FA_FEEDBACK_ID FOREIGN KEY (FEEDBACK_ID) REFERENCES FEEDBACK_MASTER(FEEDBACK_ID) ON DELETE SET NULL
                )
            """,
            "FEEDBACK_QUESTIONS": """
                CREATE TABLE FEEDBACK_QUESTIONS (
                    QUESTION_ID VARCHAR2(64) PRIMARY KEY,
                    MODULE_NAME VARCHAR2(64),
                    QUESTION_TEXT VARCHAR2(4000),
                    QUESTION_TYPE VARCHAR2(32) DEFAULT 'STARS_1_5',
                    IS_REQUIRED NUMBER(1) DEFAULT 1,
                    DISPLAY_ORDER NUMBER(5) DEFAULT 1,
                    CREATED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """,
            "FEEDBACK_RESPONSES": """
                CREATE TABLE FEEDBACK_RESPONSES (
                    RESPONSE_ID VARCHAR2(64) PRIMARY KEY,
                    FEEDBACK_ID VARCHAR2(64),
                    ACTIVITY_ID VARCHAR2(64),
                    QUESTION_ID VARCHAR2(64),
                    RATING_VALUE NUMBER(5, 2),
                    TEXT_ANSWER VARCHAR2(4000),
                    FILE_URL VARCHAR2(512),
                    SUBMITTED_AT TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT FK_FR_FEEDBACK_ID FOREIGN KEY (FEEDBACK_ID) REFERENCES FEEDBACK_MASTER(FEEDBACK_ID) ON DELETE CASCADE,
                    CONSTRAINT FK_FR_ACTIVITY_ID FOREIGN KEY (ACTIVITY_ID) REFERENCES FEEDBACK_ACTIVITY(ACTIVITY_ID) ON DELETE SET NULL,
                    CONSTRAINT FK_FR_QUESTION_ID FOREIGN KEY (QUESTION_ID) REFERENCES FEEDBACK_QUESTIONS(QUESTION_ID) ON DELETE CASCADE
                )
            """,
            "FEEDBACK_AUDIT_LOGS": """
                CREATE TABLE FEEDBACK_AUDIT_LOGS (
                    LOG_ID VARCHAR2(64) PRIMARY KEY,
                    FEEDBACK_ID VARCHAR2(64),
                    ACTION VARCHAR2(32),
                    ACTOR VARCHAR2(128),
                    DETAILS VARCHAR2(4000),
                    TIMESTAMP TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
        }

        for table_name, ddl in tables.items():
            # Check if table exists
            cursor.execute(
                "SELECT COUNT(*) FROM user_tables WHERE table_name = :tname",
                {"tname": table_name.upper()}
            )
            count = cursor.fetchone()[0]
            if count == 0:
                print(f"Creating table {table_name}...")
                cursor.execute(ddl)
                print(f"Table {table_name} created successfully.")
            else:
                print(f"Table {table_name} already exists.")

        conn.commit()

        # Seed dynamic questions
        seed_questions = [
            # General / Default
            ("Q_GEN_1", "ALL", "Overall experience and satisfaction with ChakoraHub platform?", "STARS_1_5", 1, 1),
            ("Q_GEN_2", "ALL", "Any suggestions or areas of improvement?", "TEXT", 0, 2),
            ("Q_GEN_3", "ALL", "How likely are you to recommend ChakoraHub to a friend or colleague? (NPS 0-10)", "NPS", 0, 3),

            # Course Questions
            ("Q_CRS_1", "Course", "How would you rate the course content and structure?", "STARS_1_5", 1, 1),
            ("Q_CRS_2", "Course", "Rate the clarity and depth of instructor explanations.", "STARS_1_5", 1, 2),
            ("Q_CRS_3", "Course", "Share your detailed comments about this course.", "TEXT", 0, 3),

            # Meeting Questions
            ("Q_MTG_1", "Meeting", "Was the meeting conducted on time and fully resolved your query?", "STARS_1_5", 1, 1),
            ("Q_MTG_2", "Meeting", "Rate the mentor's guidance and interaction quality.", "STARS_1_5", 1, 2),
            ("Q_MTG_3", "Meeting", "Additional feedback for your meeting session.", "TEXT", 0, 3),

            # Internship Questions
            ("Q_INT_1", "Internship", "How valuable was the hands-on project experience?", "STARS_1_5", 1, 1),
            ("Q_INT_2", "Internship", "Rate the mentorship and team support during your internship.", "STARS_1_5", 1, 2),
            ("Q_INT_3", "Internship", "What key skills did you gain or wish were covered more?", "TEXT", 0, 3),

            # Placement Questions
            ("Q_PLC_1", "Placement", "How helpful was the placement assistance and interview preparation?", "STARS_1_5", 1, 1),
            ("Q_PLC_2", "Placement", "Rate your interviewer and interview feedback experience.", "STARS_1_5", 1, 2),
            ("Q_PLC_3", "Placement", "Share details of your placement interview experience.", "TEXT", 0, 3),

            # Mock Test Questions
            ("Q_TST_1", "Mock Test", "Was the mock test difficulty and question quality appropriate?", "STARS_1_5", 1, 1),
            ("Q_TST_2", "Mock Test", "Rate the clarity of solution explanations and performance analysis.", "STARS_1_5", 1, 2),

            # Store Questions
            ("Q_STR_1", "Store Purchase", "How satisfied are you with your store purchase and delivery?", "STARS_1_5", 1, 1),
            ("Q_STR_2", "Store Purchase", "Product quality and value for money rating.", "STARS_1_5", 1, 2),
        ]

        print("Seeding dynamic questions...")
        for q_id, mod_name, q_text, q_type, req, order in seed_questions:
            cursor.execute(
                "SELECT COUNT(*) FROM FEEDBACK_QUESTIONS WHERE QUESTION_ID = :qid",
                {"qid": q_id}
            )
            if cursor.fetchone()[0] == 0:
                cursor.execute(
                    """
                    INSERT INTO FEEDBACK_QUESTIONS 
                    (QUESTION_ID, MODULE_NAME, QUESTION_TEXT, QUESTION_TYPE, IS_REQUIRED, DISPLAY_ORDER) 
                    VALUES (:1, :2, :3, :4, :5, :6)
                    """,
                    (q_id, mod_name, q_text, q_type, req, order)
                )

        conn.commit()
        print("Initialization completed successfully!")
        cursor.close()
        conn.close()

    except Exception as e:
        print(f"ERROR initializing Oracle feedback tables: {e}")
        sys.exit(1)

if __name__ == "__main__":
    init_tables()
