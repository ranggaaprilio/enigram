import { Controller, Get, Param, UseGuards } from '@nestjs/common';
import { AuthGuard } from '@nestjs/passport';
import { AppService } from './app.service';
import { Roles } from './roles.decorator';
import { RolesGuard } from './roles.guard';

@Roles('admin')
@UseGuards(AuthGuard('jwt'), RolesGuard)
@Controller('users')
export class AppController {
  constructor(private readonly service: AppService) {}

  @Get(':id')
  find(@Param('id') id: string): string {
    return this.service.find(id);
  }
}
