# 交通衔接运营数据服务

服务登记多来源事件、业务主体及现有摘要，并保存发布报告的原始内容。示例数据为虚构记录。

需要 Python 3.10 或更新版本；仅依赖标准库。

运行测试：python -m unittest discover -s tests -v
编译检查：python -m compileall -q analytics tests
本地演练：python -m analytics --database demo.sqlite3 --fixture fixtures/sample.json

analytics/store.py 保存来源事件和修订；identity.py 管理外部身份映射；metrics.py 汇总已映射主体的事件计数与通用金额；decisions.py 保存已发布报告；domain.py 维护联程行程与站内最短换乘时间目录；connections.py 在事件修订与行程目录之上形成衔接风险分析；service.py 协调这些接口。原始摘要只表示观察记录，行业结论由衔接风险分析单独给出。

衔接风险分析口径：车票事件按旅客（主体映射解析，未映射按 来源/外部标识）与行程关联，同一旅客多张联程票只计一名滞留旅客；运行时刻取同一车次同一车站的最新修订，并与最早观察到的基线对比，判定衔接中断由哪段时刻变化触发；已办理改签的（旅客, 行程）视为已处置；替代席位按同一换乘站可衔接车次的 seats 合计与仍需处置旅客数核对。

- service.assess_connections() 给出当前清单：断点、触发时刻变化、仍需处置旅客、席位覆盖。
- service.publish_handling_list() 下发处置清单，固化当时依据（行程、换乘时间、时刻修订号、车票与改签事件），同编号不接受不同内容。
- service.handling_delta() 用新到运行消息更新当前清单，对比最近一次下发的清单，给出需要重新通知的旅客批次（new/changed/cleared/unchanged）。
