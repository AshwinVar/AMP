export type IndustrialDevice = {
  id: number;
  device_code: string;
  device_name: string;
  device_type: string;
  protocol: string;
  ip_address?: string | null;
  // Reported as stored for old rows; the API refuses a value and nothing
  // routes by it (MQTT routes per tenant and site, ADR-0011). Never render it
  // as configuration.
  topic?: string | null;
  linked_machine_id?: number | null;
  status: string;
  created_at?: string;
};

export type IndustrialSignal = {
  id: number;
  device_id: number;
  machine_id?: number | null;
  signal_name: string;
  signal_value: string;
  numeric_value: number;
  unit?: string | null;
  quality: string;
  source_protocol: string;
  created_at?: string;
};

export type PlcSignalMapping = {
  id: number;
  mapping_code: string;
  device_id: number;
  source_signal: string;
  mes_field: string;
  transform_rule?: string | null;
  enabled: string;
  created_at?: string;
};

export type IndustrialGatewayAnalytics = {
  devices: number;
  online_devices: number;
  offline_devices: number;
  signals: number;
  mappings: number;
  enabled_mappings: number;
  latest_signals: {
    device_id: number;
    device_name: string;
    machine_name: string;
    signal_name: string;
    signal_value: string;
    numeric_value: number;
    unit?: string | null;
    quality: string;
    source_protocol: string;
    created_at?: string;
  }[];
};
