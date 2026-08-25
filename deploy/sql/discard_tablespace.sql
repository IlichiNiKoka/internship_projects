-- 分离表空间，为 .ibd 导入做准备
USE `sparcs_discharge_2021`;
ALTER TABLE `sparcs_discharge_2021` DISCARD TABLESPACE;
