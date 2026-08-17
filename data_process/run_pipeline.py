"""入口脚本：运行数据清洗流水线。

用法（在本目录下执行）：
    python run_pipeline.py

它只是对 medical_data_pipeline.run() 的一层薄封装，便于一眼看出
“这个项目的启动入口在哪”；真正逻辑都在 medical_data_pipeline.py。
"""

import medical_data_pipeline


if __name__ == "__main__":
    medical_data_pipeline.run()
