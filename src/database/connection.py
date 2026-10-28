from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from config.settings import settings
from .models import Base


def get_engine(database_url: str = settings.DATABASE_URL):
    # Ensure directory exists for sqlite
    if database_url.startswith("sqlite:///"):
        db_path = database_url.replace("sqlite:///", "")
        if not Path(db_path).is_absolute():
            abs_path = settings.BASE_DIR / db_path
        else:
            abs_path = Path(db_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        # Use abs_path in SQLite URL
        final_url = f"sqlite:///{abs_path.as_posix()}"
        connect_args = {"check_same_thread": False, "timeout": 5.0}
    else:
        final_url = database_url
        connect_args = {}

    engine = create_engine(
        final_url,
        connect_args=connect_args,
        echo=False,
    )

    if "sqlite" in final_url:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

    return engine


engine = get_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name == 'sqlite':
        with engine.begin() as db:
            columns={row[1] for row in db.exec_driver_sql('PRAGMA table_info(projects)')}
            if 'source_upload_id' not in columns:
                db.exec_driver_sql('ALTER TABLE projects ADD COLUMN source_upload_id VARCHAR(64)')
            db.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_source_upload ON projects(source_upload_id)')
            job_columns={row[1] for row in db.exec_driver_sql('PRAGMA table_info(jobs)')}
            for name,kind in {'current_attempt_id':'INTEGER NOT NULL DEFAULT 0','active_attempt_id':'INTEGER NOT NULL DEFAULT 0','current_fencing_token':'INTEGER NOT NULL DEFAULT 0','input_config_version':'VARCHAR(64)','interrupted_stage':'VARCHAR(64)','interrupted_reason_code':'VARCHAR(64)','retained_summary':'JSON'}.items():
                if name not in job_columns:db.exec_driver_sql('ALTER TABLE jobs ADD COLUMN '+name+' '+kind)



def get_db_session() -> Session:
    db = SessionLocal()
    try:
        return db
    finally:
        pass
