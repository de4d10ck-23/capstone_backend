-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Table: users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name TEXT NOT NULL,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'city_health_officer', 'sanitization_inspector', 'barangay_official', 'resident')),
    barangay TEXT,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW()),
    last_login TIMESTAMP WITH TIME ZONE
);

-- Table: water_locations
CREATE TABLE water_locations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    full_name TEXT NOT NULL,
    barangay TEXT,
    latitude FLOAT NOT NULL,
    longitude FLOAT NOT NULL,
    coliform_bacteria BOOLEAN,
    e_coli BOOLEAN,
    bacteriological_exam TEXT CHECK (bacteriological_exam IN ('passed', 'failed', 'untested', NULL)),
    image_url TEXT,
    sample_date DATE,
    sample_time TIME,
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    inspector_id UUID REFERENCES users(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'pending',
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW()),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW())
);

-- Table: households (for heatmap data)
CREATE TABLE households (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    latitude FLOAT NOT NULL,
    longitude FLOAT NOT NULL,
    toilet_facility INTEGER,
    barangay_code TEXT
);

-- Table: reports
CREATE TABLE reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    barangay TEXT,
    period_start DATE,
    period_end DATE,
    generated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'pending',
    file_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW())
);

-- Table: notifications
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    message TEXT,
    type TEXT DEFAULT 'info',
    barangay TEXT,
    target_roles TEXT[],
    created_by UUID REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW())
);

-- Table: inspection_requests
CREATE TABLE inspection_requests (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    location_id UUID REFERENCES water_locations(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    priority TEXT DEFAULT 'medium',
    latitude FLOAT,
    longitude FLOAT,
    barangay TEXT,
    requested_by UUID REFERENCES users(id) ON DELETE SET NULL,
    assigned_to UUID REFERENCES users(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'assigned', 'in_progress', 'completed')),
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW())
);

-- Table: resident_reports
CREATE TABLE resident_reports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    type TEXT DEFAULT 'concern',
    latitude FLOAT,
    longitude FLOAT,
    barangay TEXT,
    submitted_by UUID REFERENCES users(id) ON DELETE SET NULL,
    actioned_by UUID REFERENCES users(id) ON DELETE SET NULL,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'validated', 'rejected', 'escalated')),
    reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW())
);

-- Table: forecast_config
CREATE TABLE forecast_config (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    rainfall_weight FLOAT DEFAULT 0.3,
    temperature_weight FLOAT DEFAULT 0.2,
    humidity_weight FLOAT DEFAULT 0.15,
    proximity_weight FLOAT DEFAULT 0.2,
    historical_weight FLOAT DEFAULT 0.15,
    risk_threshold_low FLOAT DEFAULT 0.3,
    risk_threshold_high FLOAT DEFAULT 0.7,
    prediction_days INTEGER DEFAULT 7,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT TIMEZONE('utc'::text, NOW())
);

-- Function for household clusters (port of old backend logic)
CREATE OR REPLACE FUNCTION get_household_clusters()
RETURNS TABLE (
    longitude FLOAT,
    latitude FLOAT,
    toilet_facility INTEGER,
    barangay_code TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT h.longitude, h.latitude, h.toilet_facility, h.barangay_code
    FROM households h
    WHERE h.longitude IS NOT NULL AND h.latitude IS NOT NULL
      AND h.longitude BETWEEN 124.7 AND 125.1
      AND h.latitude BETWEEN 10.0 AND 10.3
    LIMIT 5000;
END;
$$ LANGUAGE plpgsql;
