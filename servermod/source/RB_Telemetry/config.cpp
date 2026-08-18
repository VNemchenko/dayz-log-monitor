class CfgPatches
{
    class RB_Telemetry
    {
        units[] = {};
        weapons[] = {};
        requiredVersion = 0.1;
        requiredAddons[] = {"DZ_Data"};
    };
};

class CfgMods
{
    class RB_Telemetry
    {
        dir = "RB_Telemetry";
        name = "Red Bastion Telemetry";
        author = "Red Bastion";
        version = "1.0.0";
        type = "mod";
        dependencies[] = {"Game", "World", "Mission"};

        class defs
        {
            class worldScriptModule
            {
                value = "";
                files[] = {"RB_Telemetry/scripts/4_World"};
            };
            class missionScriptModule
            {
                value = "";
                files[] = {"RB_Telemetry/scripts/5_Mission"};
            };
        };
    };
};
