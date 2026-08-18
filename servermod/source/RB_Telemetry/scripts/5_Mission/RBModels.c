class RBT_EventPoint
{
    string id;
    string kind;
    string event_name;
    string group_name;
    float x;
    float z;
    float radius;
    ref array<string> anchor_types;

    void RBT_EventPoint()
    {
        anchor_types = new array<string>;
    }
}

class RBT_Config
{
    int schema;
    string server_id;
    string catalog_revision;
    int snapshot_interval_ms;
    int snapshot_start_delay_ms;
    int scanner_start_delay_ms;
    int scan_tick_ms;
    int scan_batch_size;
    int missing_scans_to_end;
    ref array<ref RBT_EventPoint> event_points;

    void RBT_Config()
    {
        event_points = new array<ref RBT_EventPoint>;
    }

    bool Validate(out string error)
    {
        if (schema != 1)
        {
            error = string.Format("schema must be 1, got %1", schema);
            return false;
        }
        if (server_id.Length() < 1 || server_id.Length() > 80)
        {
            error = "server_id length must be between 1 and 80";
            return false;
        }
        if (catalog_revision.Length() != 71 || catalog_revision.Substring(0, 7) != "sha256:")
        {
            error = "catalog_revision must use sha256: followed by a 64-character digest";
            return false;
        }
        if (snapshot_interval_ms < 30000 || snapshot_interval_ms > 300000)
        {
            error = "snapshot_interval_ms must be between 30000 and 300000";
            return false;
        }
        if (snapshot_start_delay_ms < 0 || snapshot_start_delay_ms > 300000)
        {
            error = "snapshot_start_delay_ms must be between 0 and 300000";
            return false;
        }
        if (scanner_start_delay_ms < 10000 || scanner_start_delay_ms > 600000)
        {
            error = "scanner_start_delay_ms must be between 10000 and 600000";
            return false;
        }
        if (scan_tick_ms < 250 || scan_tick_ms > 10000)
        {
            error = "scan_tick_ms must be between 250 and 10000";
            return false;
        }
        if (scan_batch_size < 1 || scan_batch_size > 10)
        {
            error = "scan_batch_size must be between 1 and 10";
            return false;
        }
        if (missing_scans_to_end < 2 || missing_scans_to_end > 5)
        {
            error = "missing_scans_to_end must be between 2 and 5";
            return false;
        }
        if (!event_points || event_points.Count() < 1 || event_points.Count() > 500)
        {
            error = "event_points count must be between 1 and 500";
            return false;
        }

        ref map<string, bool> ids = new map<string, bool>;
        foreach (RBT_EventPoint point : event_points)
        {
            if (!point)
            {
                error = "event_points contains null";
                return false;
            }
            if (point.id.Length() < 1 || point.kind.Length() < 1 || point.event_name.Length() < 1)
            {
                error = "event point id, kind and event_name must not be empty";
                return false;
            }
            if (ids.Contains(point.id))
            {
                error = string.Format("duplicate event point id: %1", point.id);
                return false;
            }
            ids.Insert(point.id, true);
            if (point.x < -20000 || point.x > 20000 || point.z < -20000 || point.z > 20000)
            {
                error = string.Format("event point %1 is outside coordinate bounds", point.id);
                return false;
            }
            if (point.radius < 1 || point.radius > 60)
            {
                error = string.Format("event point %1 radius must be between 1 and 60", point.id);
                return false;
            }
            if (!point.anchor_types || point.anchor_types.Count() < 1 || point.anchor_types.Count() > 16)
            {
                error = string.Format("event point %1 must have 1..16 anchor types", point.id);
                return false;
            }
            foreach (string anchor_type : point.anchor_types)
            {
                if (anchor_type.Length() < 1 || anchor_type.Length() > 160)
                {
                    error = string.Format("event point %1 has an invalid anchor type", point.id);
                    return false;
                }
            }
        }
        return true;
    }
}

class RBT_EnvelopeBase
{
    int schema;
    string type;
    string ts_utc;
    string server_id;
    string boot_id;
    int seq;
    string catalog_revision;
    string visibility;
}

class RBT_LifecycleData
{
    string status;
    string code;
    string message;
    string dayz_baseline;
}

class RBT_LifecycleEnvelope : RBT_EnvelopeBase
{
    ref RBT_LifecycleData data;
}

class RBT_GameClock
{
    int year;
    int month;
    int day;
    int hour;
    int minute;
    float decimal_hour;
    bool is_night;
}

class RBT_Phenomenon
{
    float actual;
    float forecast;
    float next_change_seconds;
}

class RBT_WeatherSnapshot
{
    ref RBT_Phenomenon overcast;
    ref RBT_Phenomenon rain;
    ref RBT_Phenomenon fog;
    ref RBT_Phenomenon snowfall;
    ref RBT_Phenomenon wind_magnitude;
    ref RBT_Phenomenon wind_direction;
    float base_environment_temperature_c;
}

class RBT_ServerSnapshot
{
    int online_players;
    float fps_min;
    float fps_max;
    float fps_avg;
    float uptime_seconds;
}

class RBT_WorldSnapshotData
{
    ref RBT_GameClock game_time;
    ref RBT_WeatherSnapshot weather;
    ref RBT_ServerSnapshot server;
}

class RBT_WorldSnapshotEnvelope : RBT_EnvelopeBase
{
    ref RBT_WorldSnapshotData data;
}

class RBT_WorldEventData
{
    string point_id;
    string kind;
    string event_name;
    string group_name;
    float x;
    float z;
    float radius;
    string anchor_type;
    string contamination_stage;
    float remaining_seconds;
}

class RBT_WorldEventEnvelope : RBT_EnvelopeBase
{
    ref RBT_WorldEventData data;
}

class RBT_EventState
{
    ref RBT_EventPoint point;
    bool initialized;
    bool present;
    int missing_scans;
    string anchor_type;
    int contamination_stage;

    void RBT_EventState(RBT_EventPoint event_point)
    {
        point = event_point;
        contamination_stage = -1;
    }
}
