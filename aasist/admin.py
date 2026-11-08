import psycopg
from sqladmin import Admin, ModelView
from sqlalchemy import create_engine
from sqlalchemy.ext.automap import automap_base


def setup_admin(app):
    """Expose read-only views of predictions and call records using the existing schema."""
    engine = create_engine(
        "postgresql+psycopg://", creator=psycopg.connect, pool_pre_ping=True
    )
    base = automap_base()
    base.prepare(autoload_with=engine, reflection_options={
        "only": ["experiment_calls", "experiment_chunks", "experiment_predictions", "experiment_rtp_samples", "cdr"],
    })

    class ExperimentPredictionAdmin(ModelView, model=base.classes.experiment_predictions):
        name = "Experiment prediction"
        name_plural = "Experiment predictions"
        column_list = "__all__"
        column_searchable_list = ["source_filename", "prediction", "model"]
        column_default_sort = ("id", True)
        can_create = False
        can_edit = False
        can_delete = False

    class ExperimentCallAdmin(ModelView, model=base.classes.experiment_calls):
        name = "Experiment call"
        name_plural = "Experiment calls"
        column_list = "__all__"
        column_searchable_list = ["source_filename", "stream_status"]
        can_create = False
        can_edit = False
        can_delete = False

    class ExperimentChunkAdmin(ModelView, model=base.classes.experiment_chunks):
        name = "Experiment chunk"
        name_plural = "Experiment chunks"
        column_list = "__all__"
        column_default_sort = ("id", True)
        can_create = False
        can_edit = False
        can_delete = False

    class RtpSampleAdmin(ModelView, model=base.classes.experiment_rtp_samples):
        name = "RTP sample"
        name_plural = "RTP samples"
        column_list = "__all__"
        column_default_sort = ("id", True)
        can_create = False
        can_edit = False
        can_delete = False

    class CallRecordAdmin(ModelView, model=base.classes.cdr):
        name = "Call record"
        name_plural = "Call records"
        column_list = [
            "id", "start", "src", "dst", "disposition", "duration", "billsec", "userfield",
        ]
        column_searchable_list = ["src", "dst", "uniqueid", "userfield"]
        column_default_sort = ("id", True)
        can_create = False
        can_edit = False
        can_delete = False

    admin = Admin(app, engine, title="AASIST Admin")
    admin.add_view(ExperimentPredictionAdmin)
    admin.add_view(ExperimentCallAdmin)
    admin.add_view(ExperimentChunkAdmin)
    admin.add_view(RtpSampleAdmin)
    admin.add_view(CallRecordAdmin)
    return engine
