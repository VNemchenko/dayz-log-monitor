class RBT_TelemetryService
{
    static const string CONFIG_PATH = "$profile:RBTelemetry/config.json";

    protected ref RBT_Config m_Config;
    protected ref RBT_AdminLogWriter m_Writer;
    protected ref RBT_WorldSampler m_Sampler;
    protected ref RBT_EventScanner m_Scanner;
    protected bool m_Started;

    void Start()
    {
        if (m_Started)
            return;

        m_Config = new RBT_Config;
        string load_error;
        if (!JsonFileLoader<RBT_Config>.LoadFile(CONFIG_PATH, m_Config, load_error))
        {
            m_Writer = new RBT_AdminLogWriter("unconfigured", "unavailable");
            m_Writer.WriteLifecycle("telemetry.error", "disabled", "config_load_failed", load_error);
            return;
        }

        string validation_error;
        if (!m_Config.Validate(validation_error))
        {
            m_Writer = new RBT_AdminLogWriter(m_Config.server_id, m_Config.catalog_revision);
            m_Writer.WriteLifecycle("telemetry.error", "disabled", "config_invalid", validation_error);
            return;
        }

        m_Writer = new RBT_AdminLogWriter(m_Config.server_id, m_Config.catalog_revision);
        m_Sampler = new RBT_WorldSampler(m_Config, m_Writer);
        m_Scanner = new RBT_EventScanner(m_Config, m_Writer);
        m_Started = true;

        m_Writer.WriteLifecycle("telemetry.started", "running", "", "server-only telemetry started");
        m_Sampler.Start();
        m_Scanner.Start();
    }

    void Stop()
    {
        if (!m_Started)
            return;
        m_Started = false;

        if (m_Sampler)
            m_Sampler.Stop();
        if (m_Scanner)
            m_Scanner.Stop();
        if (m_Writer)
            m_Writer.WriteLifecycle("telemetry.stopped", "stopped", "", "server-only telemetry stopped");
    }
}
