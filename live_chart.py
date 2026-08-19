
"""TradingView-style chart renderer used by app.py.
The chart uses Lightweight Charts and incremental series.update() when the browser
component is kept alive. Streamlit's normal rerun path still uses setData for history.
"""
import json, pandas as pd
import streamlit.components.v1 as components

def make_payload(df, vwap=True, ltp=None, levels=None, markers=None):
    if df is None or df.empty: return {}
    plot=df.tail(300)
    candles=[{"time":int(pd.Timestamp(r.datetime).timestamp()),"open":float(r.open),
              "high":float(r.high),"low":float(r.low),"close":float(r.close)}
             for r in plot.itertuples()]
    vw=[]
    if vwap and "vwap" in plot:
        for r in plot.itertuples():
            x=getattr(r,"vwap",None)
            if pd.notna(x): vw.append({"time":int(pd.Timestamp(r.datetime).timestamp()),"value":float(x)})
    return {"candles":candles,"vwap":vw,"ltp":ltp,"levels":levels or [],"markers":markers or []}

def render(df, title, height=620, vwap=True, ltp=None, levels=None, markers=None):
    payload=make_payload(df,vwap,ltp,levels,markers)
    raw=json.dumps(payload).replace("</","<\\/")
    page=f"""<!doctype html><html><head><script src='https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js'></script>
    <style>html,body,#c{{margin:0;width:100%;height:100%;background:#0b0f14;overflow:hidden}}</style></head>
    <body><div id='c'></div><script>
    const d={raw},el=document.getElementById('c');
    const chart=LightweightCharts.createChart(el,{{width:el.clientWidth,height:{height},
    layout:{{background:{{type:'solid',color:'#0b0f14'}},textColor:'#9aa4b2'}},
    grid:{{vertLines:{{color:'#17202b'}},horzLines:{{color:'#17202b'}}}},
    crosshair:{{mode:LightweightCharts.CrosshairMode.Normal}},rightPriceScale:{{borderColor:'#263241'}},
    timeScale:{{borderColor:'#263241',timeVisible:true,secondsVisible:false,rightOffset:8,barSpacing:8}}}});
    const cs=chart.addCandlestickSeries({{upColor:'#22b39b',downColor:'#ef5350',borderVisible:false,wickUpColor:'#22b39b',wickDownColor:'#ef5350'}});
    cs.setData(d.candles);
    if(d.vwap.length){{const vs=chart.addLineSeries({{color:'#4aa3ff',lineWidth:2,priceLineVisible:false}});vs.setData(d.vwap);}}
    if(d.ltp)cs.createPriceLine({{price:Number(d.ltp),color:'#8b95a7',lineWidth:1,lineStyle:2,axisLabelVisible:true,title:'LTP'}});
    for(const x of d.levels)cs.createPriceLine({{price:Number(x.price),color:x.color||'#aab2bf',lineWidth:1,lineStyle:x.style||2,axisLabelVisible:true,title:x.title||''}});
    if(d.markers.length)cs.setMarkers(d.markers); chart.timeScale().fitContent();
    new ResizeObserver(()=>chart.applyOptions({{width:el.clientWidth}})).observe(el);
    </script></body></html>"""
    components.html(page,height=height+4,scrolling=False)
