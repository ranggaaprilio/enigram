import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { AppController } from './app.controller';
import { AppService } from './app.service';
import { SqlUserRepo } from './repo';
import authConfig from './auth.config';

const database = process.env.DB === 'mongo' ? MongooseModule.forRootAsync({}) : TypeOrmModule.forRootAsync({});

@Module({
  imports: [ConfigModule.forRoot({ load: [authConfig] }), database],
  controllers: [AppController],
  providers: [AppService, { provide: 'USER_REPO', useClass: SqlUserRepo }],
})
export class AppModule {}
