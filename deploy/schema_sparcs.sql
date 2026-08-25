-- =============================================================
-- SPARCS 2021 数据底座 · 建库建表脚本（配合 .ibd 表空间导入使用）
-- 由 mysqldump 导出的真实表结构生成，勿手工改列顺序。
-- =============================================================

CREATE DATABASE IF NOT EXISTS `sparcs_discharge_2021`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE `sparcs_discharge_2021`;

DROP TABLE IF EXISTS `sparcs_discharge_2021`;
CREATE TABLE `sparcs_discharge_2021` (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `hospital_service_area` varchar(64) DEFAULT NULL COMMENT '医院服务区域',
  `hospital_county` varchar(64) DEFAULT NULL COMMENT '所在县',
  `operating_certificate_number` varchar(16) DEFAULT NULL COMMENT '运营证书号（保前导零）',
  `permanent_facility_id` varchar(16) DEFAULT NULL COMMENT '机构永久标识（保前导零）',
  `facility_name` varchar(255) DEFAULT NULL COMMENT '医院全称',
  `age_group` varchar(32) DEFAULT NULL COMMENT '年龄组',
  `zip_code_3_digits` varchar(8) DEFAULT NULL COMMENT '邮编前缀（含 OOS 州外）',
  `gender` varchar(16) DEFAULT NULL COMMENT '性别 Male/Female/Unknown',
  `race` varchar(64) DEFAULT NULL COMMENT '种族',
  `ethnicity` varchar(64) DEFAULT NULL COMMENT '族裔',
  `type_of_admission` varchar(32) DEFAULT NULL COMMENT '入院类型',
  `patient_disposition` varchar(128) DEFAULT NULL COMMENT '出院去向',
  `ccsr_diagnosis_code` varchar(16) DEFAULT NULL COMMENT 'CCSR 诊断编码',
  `ccsr_diagnosis_description` varchar(255) DEFAULT NULL COMMENT 'CCSR 诊断描述',
  `ccsr_procedure_code` varchar(16) DEFAULT NULL COMMENT 'CCSR 操作编码',
  `ccsr_procedure_description` varchar(255) DEFAULT NULL COMMENT 'CCSR 操作描述',
  `apr_drg_code` varchar(16) DEFAULT NULL COMMENT 'APR DRG 编码（保前导零）',
  `apr_drg_description` varchar(255) DEFAULT NULL COMMENT 'APR DRG 描述',
  `apr_mdc_code` varchar(16) DEFAULT NULL COMMENT 'APR MDC 编码（保前导零）',
  `apr_mdc_description` varchar(255) DEFAULT NULL COMMENT 'APR MDC 描述',
  `apr_severity_of_illness_description` varchar(32) DEFAULT NULL COMMENT '病情严重程度描述',
  `apr_risk_of_mortality` varchar(32) DEFAULT NULL COMMENT '死亡风险（文本字段）',
  `apr_medical_surgical_description` varchar(32) DEFAULT NULL COMMENT '内外科标志',
  `payment_typology_1` varchar(64) DEFAULT NULL COMMENT '支付方式(主)',
  `payment_typology_2` varchar(64) DEFAULT NULL COMMENT '支付方式(次)',
  `payment_typology_3` varchar(64) DEFAULT NULL COMMENT '支付方式(三)',
  `emergency_department_indicator` varchar(8) DEFAULT NULL COMMENT '急诊标志 Y/N',
  `length_of_stay` int DEFAULT NULL COMMENT '住院天数',
  `discharge_year` smallint DEFAULT NULL COMMENT '出院年份',
  `apr_severity_of_illness_code` tinyint DEFAULT NULL COMMENT '病情严重程度代码 0~4',
  `birth_weight` int DEFAULT NULL COMMENT '出生体重（克）',
  `total_charges` double DEFAULT NULL COMMENT '总费用（美元）',
  `total_costs` double DEFAULT NULL COMMENT '总成本（美元）',
  `row_hash` char(20) NOT NULL COMMENT '整行内容哈希（唯一键）',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_row_hash` (`row_hash`),
  KEY `idx_discharge_year` (`discharge_year`),
  KEY `idx_hospital_county` (`hospital_county`),
  KEY `idx_ccsr_diagnosis_code` (`ccsr_diagnosis_code`),
  KEY `idx_age_group` (`age_group`),
  KEY `idx_gender` (`gender`),
  KEY `idx_payment_typology_1` (`payment_typology_1`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='SPARCS 2021 清洗后住院数据';
