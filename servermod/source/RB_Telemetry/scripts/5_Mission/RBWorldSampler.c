class RBT_WorldSampler
{
    protected ref RBT_Config m_Config;
    protected ref RBT_AdminLogWriter m_Writer;
    protected bool m_Running;

    void RBT_WorldSampler(RBT_Config config, RBT_AdminLogWriter writer)
    {
        m_Config = config;
        m_Writer = writer;
    }

    void Start()
    {
        if (m_Running)
            return;
        m_Running = true;
        g_Game.GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(Sample, m_Config.snapshot_start_delay_ms, false);
    }

    void Stop()
    {
        if (!m_Running)
            return;
        m_Running = false;
        g_Game.GetCallQueue(CALL_CATEGORY_SYSTEM).Remove(Sample);
    }

    protected RBT_Phenomenon ReadPhenomenon(WeatherPhenomenon phenomenon)
    {
        ref RBT_Phenomenon result = new RBT_Phenomenon;
        result.actual = phenomenon.GetActual();
        result.forecast = phenomenon.GetForecast();
        result.next_change_seconds = phenomenon.GetNextChange();
        return result;
    }

    void Sample()
    {
        if (!m_Running)
            return;

        ref RBT_WorldSnapshotData snapshot = new RBT_WorldSnapshotData;
        snapshot.game_time = new RBT_GameClock;
        snapshot.weather = new RBT_WeatherSnapshot;
        snapshot.server = new RBT_ServerSnapshot;

        g_Game.GetWorld().GetDate(
            snapshot.game_time.year,
            snapshot.game_time.month,
            snapshot.game_time.day,
            snapshot.game_time.hour,
            snapshot.game_time.minute
        );
        snapshot.game_time.decimal_hour = g_Game.GetDayTime();
        snapshot.game_time.is_night = g_Game.GetWorld().IsNight();

        Weather weather = g_Game.GetWeather();
        snapshot.weather.overcast = ReadPhenomenon(weather.GetOvercast());
        snapshot.weather.rain = ReadPhenomenon(weather.GetRain());
        snapshot.weather.fog = ReadPhenomenon(weather.GetFog());
        snapshot.weather.snowfall = ReadPhenomenon(weather.GetSnowfall());
        snapshot.weather.wind_magnitude = ReadPhenomenon(weather.GetWindMagnitude());
        snapshot.weather.wind_direction = ReadPhenomenon(weather.GetWindDirection());
        snapshot.weather.base_environment_temperature_c = g_Game.GetMission().GetWorldData().GetBaseEnvTemperature();

        ref array<Man> players = new array<Man>;
        g_Game.GetPlayers(players);
        snapshot.server.online_players = players.Count();
        g_Game.GetFPSStats(
            snapshot.server.fps_min,
            snapshot.server.fps_max,
            snapshot.server.fps_avg
        );
        snapshot.server.uptime_seconds = g_Game.GetTickTime();

        m_Writer.WriteSnapshot(snapshot);
        if (m_Running)
            g_Game.GetCallQueue(CALL_CATEGORY_SYSTEM).CallLater(Sample, m_Config.snapshot_interval_ms, false);
    }
}
