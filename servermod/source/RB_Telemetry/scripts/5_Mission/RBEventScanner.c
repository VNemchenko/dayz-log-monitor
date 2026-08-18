class RBT_EventScanner
{
    protected ref RBT_Config m_Config;
    protected ref RBT_AdminLogWriter m_Writer;
    protected ref array<ref RBT_EventState> m_States;
    protected int m_NextIndex;
    protected bool m_Running;

    void RBT_EventScanner(RBT_Config config, RBT_AdminLogWriter writer)
    {
        m_Config = config;
        m_Writer = writer;
        m_States = new array<ref RBT_EventState>;
        foreach (RBT_EventPoint point : m_Config.event_points)
            m_States.Insert(new RBT_EventState(point));
    }

    void Start()
    {
        if (m_Running)
            return;
        m_Running = true;
        g_Game.GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(ScanBatch, m_Config.scanner_start_delay_ms, false);
    }

    void Stop()
    {
        if (!m_Running)
            return;
        m_Running = false;
        g_Game.GetCallQueue(CALL_CATEGORY_SYSTEM).Remove(ScanBatch);
    }

    protected bool FindAnchor(RBT_EventPoint point, out Object anchor)
    {
        ref array<Object> objects = new array<Object>;
        ref array<CargoBase> proxy_cargos = new array<CargoBase>;
        vector center = Vector(point.x, 0, point.z);
        g_Game.GetObjectsAtPosition(center, point.radius, objects, proxy_cargos);

        foreach (string expected_type : point.anchor_types)
        {
            foreach (Object candidate : objects)
            {
                if (candidate && candidate.GetType() == expected_type)
                {
                    anchor = candidate;
                    return true;
                }
            }
        }
        anchor = null;
        return false;
    }

    protected int ReadContaminationStage(Object anchor, out float remaining_seconds)
    {
        remaining_seconds = -1;
        ContaminatedArea_DynamicBase area = ContaminatedArea_DynamicBase.Cast(anchor);
        if (!area)
            return -1;
        remaining_seconds = area.GetRemainingTime();
        return area.RB_Telemetry_GetDecayState();
    }

    protected string ContaminationStageName(int stage)
    {
        switch (stage)
        {
            case eAreaDecayStage.INIT:
                return "init";
            case eAreaDecayStage.START:
                return "start";
            case eAreaDecayStage.LIVE:
                return "live";
            case eAreaDecayStage.DECAY_START:
                return "decay_start";
            case eAreaDecayStage.DECAY_END:
                return "decay_end";
        }
        return "";
    }

    protected RBT_WorldEventData BuildEventData(
        RBT_EventPoint point,
        string anchor_type,
        int contamination_stage,
        float remaining_seconds
    )
    {
        ref RBT_WorldEventData event_data = new RBT_WorldEventData;
        event_data.point_id = point.id;
        event_data.kind = point.kind;
        event_data.event_name = point.event_name;
        event_data.group_name = point.group_name;
        event_data.x = point.x;
        event_data.z = point.z;
        event_data.radius = point.radius;
        event_data.anchor_type = anchor_type;
        event_data.contamination_stage = ContaminationStageName(contamination_stage);
        event_data.remaining_seconds = remaining_seconds;
        return event_data;
    }

    protected void ProcessState(RBT_EventState state)
    {
        Object anchor;
        bool found = FindAnchor(state.point, anchor);
        if (!found)
        {
            if (!state.initialized)
            {
                state.initialized = true;
                state.present = false;
                return;
            }
            if (!state.present)
                return;

            state.missing_scans++;
            if (state.missing_scans < m_Config.missing_scans_to_end)
                return;

            ref RBT_WorldEventData ended_data = BuildEventData(
                state.point,
                state.anchor_type,
                state.contamination_stage,
                -1
            );
            m_Writer.WriteWorldEvent("world.event.ended", ended_data);
            state.present = false;
            state.missing_scans = 0;
            state.anchor_type = "";
            state.contamination_stage = -1;
            return;
        }

        string current_anchor_type = anchor.GetType();
        float remaining_seconds;
        int current_stage = ReadContaminationStage(anchor, remaining_seconds);
        state.missing_scans = 0;

        if (!state.initialized)
        {
            state.initialized = true;
            state.present = true;
            state.anchor_type = current_anchor_type;
            state.contamination_stage = current_stage;
            m_Writer.WriteWorldEvent(
                "world.event.present",
                BuildEventData(state.point, current_anchor_type, current_stage, remaining_seconds)
            );
            return;
        }

        if (!state.present)
        {
            state.present = true;
            state.anchor_type = current_anchor_type;
            state.contamination_stage = current_stage;
            m_Writer.WriteWorldEvent(
                "world.event.started",
                BuildEventData(state.point, current_anchor_type, current_stage, remaining_seconds)
            );
            return;
        }

        state.anchor_type = current_anchor_type;
        if (current_stage >= 0 && current_stage != state.contamination_stage)
        {
            state.contamination_stage = current_stage;
            m_Writer.WriteWorldEvent(
                "world.event.updated",
                BuildEventData(state.point, current_anchor_type, current_stage, remaining_seconds)
            );
        }
    }

    void ScanBatch()
    {
        if (!m_Running)
            return;

        int count = m_States.Count();
        for (int i = 0; i < m_Config.scan_batch_size && i < count; i++)
        {
            ProcessState(m_States.Get(m_NextIndex));
            m_NextIndex++;
            if (m_NextIndex >= count)
                m_NextIndex = 0;
        }

        if (m_Running)
            g_Game.GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(ScanBatch, m_Config.scan_tick_ms, false);
    }
}
