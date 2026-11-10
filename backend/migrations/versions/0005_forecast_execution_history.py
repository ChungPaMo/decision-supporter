"""Forecast execution snapshots, approved input references and per-run results.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE forecast_run (
 execution_id uuid PRIMARY KEY,
 dedup_key varchar(64) NOT NULL,
 input_digest varchar(71) NOT NULL,
 parameters jsonb NOT NULL,
 input_snapshot jsonb NOT NULL,
 runtime_identity jsonb NOT NULL,
 input_row_count integer NOT NULL CHECK (input_row_count >= 0),
 forecast_count integer NOT NULL CHECK (forecast_count >= 0),
 status varchar(24) NOT NULL CHECK (status IN ('OK','PARTIAL','DATA_INSUFFICIENT','INPUT_INVALID','FAILED')),
 quantile_model_version varchar(60),
 result jsonb,
 failure jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 finished_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CONSTRAINT ck_forecast_run_result CHECK (
   (status = 'FAILED' AND result IS NULL AND failure IS NOT NULL AND forecast_count = 0)
   OR (status <> 'FAILED' AND result IS NOT NULL AND failure IS NULL AND result->>'status' = status))
);
CREATE UNIQUE INDEX uq_forecast_dedup_completed ON forecast_run(dedup_key) WHERE status <> 'FAILED';
CREATE INDEX ix_forecast_run_created ON forecast_run(created_at DESC, execution_id DESC);
CREATE TABLE forecast_run_input (
 execution_id uuid NOT NULL REFERENCES forecast_run(execution_id),
 batch_id uuid NOT NULL REFERENCES import_batch(batch_id),
 batch_version integer NOT NULL,
 source_sha256 varchar(64) NOT NULL,
 result_digest varchar(64),
 approved_at timestamptz NOT NULL,
 PRIMARY KEY (execution_id, batch_id)
);
CREATE FUNCTION check_forecast_approved_input() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM import_batch b WHERE b.batch_id = NEW.batch_id
   AND b.entity = 'monthly_demand' AND b.status = 'APPROVED' AND b.blocking_error_count = 0
   AND b.version = NEW.batch_version AND b.source_sha256 = NEW.source_sha256
   AND b.result_digest IS NOT DISTINCT FROM NEW.result_digest AND b.approved_at = NEW.approved_at) THEN
   RAISE EXCEPTION 'forecast input must reference an unchanged approved monthly demand batch'
     USING ERRCODE = 'check_violation';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER trg_forecast_approved_input BEFORE INSERT OR UPDATE ON forecast_run_input
 FOR EACH ROW EXECUTE FUNCTION check_forecast_approved_input();
ALTER TABLE demand_forecast ADD COLUMN forecast_run_id uuid REFERENCES forecast_run(execution_id);
ALTER TABLE demand_forecast DROP CONSTRAINT uq_forecast_run;
CREATE UNIQUE INDEX uq_forecast_legacy ON demand_forecast(product_id,target_month,horizon,quantile_model_version,trained_until)
 WHERE forecast_run_id IS NULL;
ALTER TABLE demand_forecast ADD CONSTRAINT uq_forecast_execution_row UNIQUE(forecast_run_id,product_id,target_month,horizon);
"""
    )


def downgrade() -> None:
    # Do not erase history or fabricate legacy provenance during rollback.
    op.execute("""
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM forecast_run) OR EXISTS (SELECT 1 FROM demand_forecast WHERE forecast_run_id IS NOT NULL) THEN
   RAISE EXCEPTION 'cannot downgrade 0005 with forecast history; preserve data';
 END IF;
END $$;
ALTER TABLE demand_forecast DROP CONSTRAINT uq_forecast_execution_row;
DROP INDEX uq_forecast_legacy;
ALTER TABLE demand_forecast ADD CONSTRAINT uq_forecast_run UNIQUE(product_id,target_month,horizon,quantile_model_version,trained_until);
ALTER TABLE demand_forecast DROP COLUMN forecast_run_id;
DROP TABLE forecast_run_input;
DROP FUNCTION check_forecast_approved_input();
DROP TABLE forecast_run;
"""
    )
