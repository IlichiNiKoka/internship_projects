-- 检查目标库中的表数量（0 = 空库，需要导入）
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema='sparcs_discharge_2021';
