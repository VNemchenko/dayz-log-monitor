class RBT_AdminLogWriter
{
    protected string m_ServerId;
    protected string m_CatalogRevision;
    protected string m_BootId;
    protected int m_Sequence;
    protected ref JsonSerializer m_Serializer;

    void RBT_AdminLogWriter(string server_id, string catalog_revision)
    {
        m_ServerId = server_id;
        m_CatalogRevision = catalog_revision;
        m_BootId = BuildBootId();
        m_Serializer = new JsonSerializer;
    }

    protected string Pad2(int value)
    {
        if (value < 10)
            return "0" + value.ToString();
        return value.ToString();
    }

    protected string NowUtc()
    {
        int year;
        int month;
        int day;
        int hour;
        int minute;
        int second;
        GetYearMonthDayUTC(year, month, day);
        GetHourMinuteSecondUTC(hour, minute, second);
        return year.ToString() + "-" + Pad2(month) + "-" + Pad2(day) + "T" + Pad2(hour) + ":" + Pad2(minute) + ":" + Pad2(second) + "Z";
    }

    protected string BuildBootId()
    {
        int year;
        int month;
        int day;
        int hour;
        int minute;
        int second;
        GetYearMonthDayUTC(year, month, day);
        GetHourMinuteSecondUTC(hour, minute, second);
        return year.ToString() + Pad2(month) + Pad2(day) + "T" + Pad2(hour) + Pad2(minute) + Pad2(second) + "Z";
    }

    protected void Prepare(RBT_EnvelopeBase envelope, string event_type)
    {
        m_Sequence++;
        envelope.schema = 1;
        envelope.type = event_type;
        envelope.ts_utc = NowUtc();
        envelope.server_id = m_ServerId;
        envelope.boot_id = m_BootId;
        envelope.seq = m_Sequence;
        envelope.catalog_revision = m_CatalogRevision;
        envelope.visibility = "admin";
    }

    protected bool SerializeAndLog(void envelope)
    {
        string serialized;
        if (!m_Serializer.WriteToString(envelope, false, serialized))
        {
            g_Game.AdminLog("RB_EVT_ERROR v1 serialization_failed");
            return false;
        }

        g_Game.AdminLog("RB_EVT v1 " + serialized);
        return true;
    }

    void WriteLifecycle(string event_type, string status, string code, string message)
    {
        ref RBT_LifecycleEnvelope envelope = new RBT_LifecycleEnvelope;
        Prepare(envelope, event_type);
        envelope.data = new RBT_LifecycleData;
        envelope.data.status = status;
        envelope.data.code = code;
        envelope.data.message = message;
        envelope.data.dayz_baseline = "1.29.163709";
        SerializeAndLog(envelope);
    }

    void WriteSnapshot(RBT_WorldSnapshotData snapshot)
    {
        ref RBT_WorldSnapshotEnvelope envelope = new RBT_WorldSnapshotEnvelope;
        Prepare(envelope, "world.snapshot");
        envelope.data = snapshot;
        SerializeAndLog(envelope);
    }

    void WriteWorldEvent(string event_type, RBT_WorldEventData event_data)
    {
        ref RBT_WorldEventEnvelope envelope = new RBT_WorldEventEnvelope;
        Prepare(envelope, event_type);
        envelope.data = event_data;
        SerializeAndLog(envelope);
    }
}
