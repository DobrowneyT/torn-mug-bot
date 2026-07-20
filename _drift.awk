#!/usr/bin/awk -f
# Measure phase drift between run0 and run1 using cache_ts_mod (col4).
# For each run/item, take the cache_ts_mod values, fold to one phase by mod 30,
# compute circular mean on a 30-wide circle, compare run0 vs run1.
function cdist30(a,b,  d){d=a-b;if(d<0)d=-d;if(d>15)d=30-d;return d}
function cmean30(n,arr,   i,sx,sy,ang,r){
  sx=0;sy=0;
  for(i=1;i<=n;i++){ang=(arr[i]%30)*6.283185307/30.0;sx+=cos(ang);sy+=sin(ang)}
  r=atan2(sy/n,sx/n)*30.0/6.283185307; if(r<0)r+=30; return r
}
BEGIN{FS="\t"}
{
  run=(FILENAME ~ /_run1/)?1:0
  id=$1; name[$1]=$2
  n=split($4,a,",")
  # skip first value as potential batch artifact only if it's an outlier; keep all for phase
  c=cmean30(n,a)
  if(run==0){p0[id]=c; have0[id]=1}
  else {p1[id]=c; have1[id]=1}
}
END{
  print "item_id\tphase0_mod30\tphase1_mod30\tdrift"
  sumabs=0; cnt=0; maxd=0
  for(id in name){
    if(have0[id] && have1[id]){
      d=cdist30(p0[id],p1[id])
      printf "%s\t%.1f\t%.1f\t%.1f\n", id, p0[id], p1[id], d
      sumabs+=d; cnt++; if(d>maxd)maxd=d
    }
  }
  printf "SUMMARY mean_abs_drift=%.2f max_drift=%.2f n=%d\n", sumabs/cnt, maxd, cnt
}
