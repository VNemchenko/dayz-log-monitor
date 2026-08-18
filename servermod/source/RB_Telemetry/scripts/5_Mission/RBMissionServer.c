modded class MissionServer
{
    protected ref RBT_TelemetryService m_RBTelemetry;

    override void OnMissionStart()
    {
        super.OnMissionStart();
        m_RBTelemetry = new RBT_TelemetryService;
        m_RBTelemetry.Start();
    }

    override void OnMissionFinish()
    {
        if (m_RBTelemetry)
            m_RBTelemetry.Stop();
        m_RBTelemetry = null;
        super.OnMissionFinish();
    }
}
