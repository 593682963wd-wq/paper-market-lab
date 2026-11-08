# 离线多策略市场模拟

保留完整K线归一、指标、市场分型、五策略、Decimal费用/滑点/P&L核算和持久化影子交易引擎；CLI使用显式输入行情。

## 使用

Python 3.11+。在本目录建立独立虚拟环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python cli.py example.json
.venv/bin/python -m unittest discover -v
```

`example.json`从零编写，仅含虚构输入。可以显式提供自己的同结构JSON文件。输出默认为终端；`--output result.json`使用独占创建，避免覆盖已有结果。CLI失败退出码2。

## 数据与边界

不发现本机账号、不读取环境中的模型密钥、不发送消息、不请求远端数据。只处理调用方明确传入的文件、记录或参数。合成示例不代表任何实际业务结果。不含交易所客户端、交易口令、实时行情、个人持仓或原股票Web界面；股票撮合子模块另列，不能把模拟结果视作真实收益。

## 发布状态

本项目源码采用 MIT 许可证，见 LICENSE。依赖按各自上游许可使用，安装的依赖/模型/字体/媒体不随此源码目录分发。

## 组件接口

附加action observe接受snapshot字段klines/ticker/order_book/recent_trades；action orders接受quotes字典与orders数组，仅接受synthetic=true的合成报价。原完整交易库提供限价/市价简化撮合、整数分账本、FIFO lot、T+1、挂单撤销与幂等校验；费用配置是零费用测试参数，不是现行收费规则。

## 2026-11-08 协作修订

- 恒价RSI保持中性。
- 旧批次不覆盖最新估值，同时间修正先更新价格。
- 时间戳缺口使模拟降级并暂停成交。
- TradeError转换CLI退出2，不连接真实账户。

集合根目录 `verify_release.py --project personal-market-simulation` 可复跑合成测试和首次使用示例；本轮以集合验证报告为准。
