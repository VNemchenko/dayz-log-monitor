// Read-only bridge for the protected vanilla state.  It never advances or
// otherwise mutates the contaminated-area lifecycle.
modded class ContaminatedArea_DynamicBase
{
    int RB_Telemetry_GetDecayState()
    {
        return m_DecayState;
    }
}
