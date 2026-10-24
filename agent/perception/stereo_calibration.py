"""Offline chessboard calibration. Metric scale requires an explicit board source."""
import hashlib
import json
import math
from pathlib import Path
import cv2
import numpy as np
from perception.capture_store import validate_session,canonical


def validate_board(board):
    for key in ('columns','rows'):
        if type(board.get(key)) is not int or not 3<=board[key]<=30:raise ValueError('必须填写真实内角点行列数')
    size=board.get('square_size_mm')
    if isinstance(size,bool) or not isinstance(size,(int,float)) or not math.isfinite(size) or size<=0:raise ValueError('缺少有效的单格实际边长，禁止默认毫米尺度')
    if not isinstance(board.get('size_source'),str) or not board['size_source'].strip():raise ValueError('必须说明格距尺寸来源')
    return board


def fit_points(object_points,left_points,right_points,image_size,validation):
    if len(object_points)<10 or len(validation)<3:raise ValueError('至少需要10个拟合图对及3个独立验证图对；正式建议30–50及10–15对')
    crit=(cv2.TERM_CRITERIA_EPS+cv2.TERM_CRITERIA_MAX_ITER,100,1e-7)
    rms_l,k1,d1,r1,t1=cv2.calibrateCamera(object_points,left_points,image_size,None,None,criteria=crit)
    rms_r,k2,d2,r2,t2=cv2.calibrateCamera(object_points,right_points,image_size,None,None,criteria=crit)
    rms,k1,d1,k2,d2,R,T,E,F=cv2.stereoCalibrate(object_points,left_points,right_points,k1,d1,k2,d2,image_size,criteria=crit,flags=cv2.CALIB_FIX_INTRINSIC)
    R1,R2,P1,P2,Q,roi1,roi2=cv2.stereoRectify(k1,d1,k2,d2,image_size,R,T,flags=cv2.CALIB_ZERO_DISPARITY,alpha=0)
    matrices=dict(K1=k1,D1=d1,K2=k2,D2=d2,R=R,T=T,E=E,F=F,R1=R1,R2=R2,P1=P1,P2=P2,Q=Q)
    if not all(np.isfinite(m).all() for m in matrices.values()):raise ValueError('标定产生非有限矩阵')
    if np.linalg.norm(T)<=0:raise ValueError('无效双目基线')
    residuals=[];per_view=[]
    for ident,lp,rp in validation:
        ul=cv2.undistortPoints(lp,k1,d1,R=R1,P=P1)
        ur=cv2.undistortPoints(rp,k2,d2,R=R2,P=P2)
        dy=np.abs(ul[:,0,1]-ur[:,0,1]);residuals.extend(dy.tolist())
        per_view.append({'event_id':ident,'vertical_residual_median_px':float(np.median(dy)),
                         'vertical_residual_p95_px':float(np.percentile(dy,95))})
    diagnostics={'monocular_rms_px':{'left':float(rms_l),'right':float(rms_r)},'stereo_rms_px':float(rms),
                 'baseline_mm':float(np.linalg.norm(T)),'validation_vertical_p95_px':float(np.percentile(residuals,95)),
                 'validation_views':per_view,'valid_roi_left':list(roi1),'valid_roi_right':list(roi2)}
    return matrices,diagnostics


def calibrate_session(session_dir,board,output,exclusions=None):
    validate_board(board)
    report=validate_session(session_dir)
    if not report['valid']:raise ValueError('采集会话完整性未通过')
    events=report['events'];exclusions={} if exclusions is None else exclusions
    known={e['event_id'] for e in events}
    if not isinstance(exclusions,dict) or set(exclusions)-known or any(not isinstance(reason,str) or not reason.strip() for reason in exclusions.values()):raise ValueError('排除清单必须由已有事件ID映射至明确理由')
    train=[e for e in events if e['purpose']=='calibration' and e['event_id'] not in exclusions]
    test=[e for e in events if e['purpose']=='validation' and e['event_id'] not in exclusions]
    if len(train)<10 or len(test)<3:raise ValueError('不足10个标定图对及3个独立验证图对')
    # Same images under different event IDs still count as leakage.
    train_hash={i['sha256'] for e in train for i in e['images'].values()}
    test_hash={i['sha256'] for e in test for i in e['images'].values()}
    if train_hash & test_hash:raise ValueError('拟合与验证图像重复，存在数据泄漏')
    pattern=(board['columns'],board['rows']);grid=np.zeros((pattern[0]*pattern[1],3),np.float32)
    grid[:,:2]=np.mgrid[0:pattern[0],0:pattern[1]].T.reshape(-1,2)*board['square_size_mm']
    objects=[];lp=[];rp=[];validation=[];failures=[];image_size=None
    root=Path(session_dir)
    for event in train+test:
        corners=[]
        for side in ('left','right'):
            img=cv2.imread(str(root/'events'/event['event_id']/(side+'.png')),cv2.IMREAD_GRAYSCALE)
            if image_size is None:image_size=(img.shape[1],img.shape[0])
            if image_size!=(img.shape[1],img.shape[0]):raise ValueError('混合图像尺寸')
            ok,c=cv2.findChessboardCornersSB(img,pattern,flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
            corners.append(c if ok else None)
        if any(c is None for c in corners):failures.append(event['event_id']);continue
        if event['purpose']=='calibration':objects.append(grid.copy());lp.append(corners[0]);rp.append(corners[1])
        else:validation.append((event['event_id'],*corners))
    if failures:raise ValueError('以下图对未完整检出角点；须明确重采或排除，不自动静默删除: '+','.join(failures))
    matrices,diagnostics=fit_points(objects,lp,rp,image_size,validation)
    body={'schema_version':'stereo-calibration-v1','opencv':cv2.__version__,'board':board,
          'image_size':list(image_size),'session_config':report['session']['config'],
          'fitting_event_ids':[e['event_id'] for e in train],'validation_event_ids':[e['event_id'] for e in test],
          'excluded_events':exclusions,
          'input_manifest':[{ 'event_id':e['event_id'],'purpose':e['purpose'],'images':{s:e['images'][s]['sha256'] for s in ('left','right')}} for e in events],
          'matrices':{k:v.tolist() for k,v in matrices.items()},'diagnostics':diagnostics,
          'status':'candidate_requires_physical_validation','physical_accuracy_validated':False,'hardware_synchronized':False}
    body['calibration_id']=hashlib.sha256(canonical(body)).hexdigest()
    with Path(output).open('xb') as f:f.write(canonical(body))
    return body
